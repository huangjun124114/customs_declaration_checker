"""环境体检脚本（tools/check_env.py，对应架构设计 13.9 · R11 防线）。

用途：
  在**打包前**（``build_exe.ps1`` 第一步）与**部署前**运行，检出可能污染 exe 的环境问题：

  1. **空壳包检测**：遍历 ``site-packages``，找出两类损坏目录（排除 ``*.libs``
     这类正常的二进制目录）：

     * 「**完全空目录**」—— ``files == []``：pip 安装尚未落盘即被中断；
     * 「**残缺包**」—— 目录里**有文件**，但**既无 ``__init__.py`` 也无同名
       ``dist-info``**：pip 安装中途被打断，关键文件（如 ``openpyxl/__init__.py``、
       ``openpyxl/pivot/table.py``）丢失，import 时才暴露
       ``cannot import name X`` / ``__file__ is None`` 等隐藏 bug。

     两类都必须报错并阻止打包 —— 残缺包更隐蔽，**最易漏检、后果最重**。
  2. **逐包 import 验证**：对关键依赖逐个 ``import``（含 `openpyxl.pivot.table`
     等**深路径子模块**），检出"能 import 包名但缺子模块/属性"的损坏情况。
  3. **版本核对**：把实测版本与架构设计 v1.2 第 13.6 节锁定版本比对，不一致给警告。

退出码：
  * ``0`` —— 通过（可继续打包）；
  * ``1`` —— 发现**致命问题**（空壳包 / 残缺包 / 关键依赖不可 import）→ 必须中止打包；
  * ``2`` —— 仅警告（版本偏差等）→ 可选择继续。

自测方式（无需真实污染环境）::

    python tools/check_env.py --scan-dir /tmp/fake_site_packages
    # 伪造 <fake>/bogus_pkg/ 空目录（无 __init__.py）→ 应报「完全空目录」并非 0 退出
    # 伪造 <fake>/broken_pkg/__init__.py 但缺关键子模块 → 应报「残缺包」并非 0 退出
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# ── 关键依赖（架构设计 v1.2 第 13.6 节锁定版本）──────────────────────
# 名称 → (import 名, 可选版本下界, 可选版本上界, 说明)
#
# 说明：R11 空壳污染已累计复发 7 次——凡"曾被打穿"或"属于运行期硬依赖"的包
# 都必须在此登记，否则 check_env 体检形同虚设（本次 openpyxl 因未登记而漏检）。
CRITICAL_DEPS: tuple[tuple[str, str, str, str, str], ...] = (
    # ── 一级直接依赖（架构设计 13.6 锁定版本）────────────────────
    ("PySide6", "PySide6", "6.9", "6.10", "GUI 框架"),
    ("rapidocr", "rapidocr", "3.9", "4.0", "OCR 封装库"),
    ("onnxruntime", "onnxruntime", "1.20", "2.0", "OCR 推理后端"),
    ("numpy", "numpy", "2.0", "3.0", "图像数组"),
    ("opencv-python-headless", "cv2", "4.10", "5.0", "图像预处理（headless）"),
    ("Pillow", "PIL", "11", "13", "图片加载"),
    ("PyYAML", "yaml", "6.0", "", "YAML 规则解析"),
    ("openpyxl", "openpyxl", "3.1", "4.0", "Excel 读写（xlsx 结果回写）"),
    # ── rapidocr 传递依赖（缺一即 ImportError）──────────────────
    ("omegaconf", "omegaconf", "2.3", "3.0", "rapidocr 依赖"),
    ("pyclipper", "pyclipper", "1.3", "", "rapidocr 依赖"),
    ("shapely", "shapely", "2.0", "", "rapidocr 依赖"),
    ("requests", "requests", "", "", "rapidocr 依赖（模型下载）"),
    ("certifi", "certifi", "", "", "rapidocr 依赖（CA 证书）"),
    ("charset-normalizer", "charset_normalizer", "", "", "requests 依赖（编码嗅探）"),
    ("antlr4-python3-runtime", "antlr4", "", "", "rapidocr 依赖（语法解析）"),
    ("flatbuffers", "flatbuffers", "", "", "onnxruntime 依赖（序列化）"),
    ("six", "six", "", "", "rapidocr 依赖（兼容层）"),
    ("tqdm", "tqdm", "", "", "rapidocr 依赖（进度条）"),
    ("colorlog", "colorlog", "", "", "rapidocr 依赖（彩色日志）"),
)

#: 需要执行 ``import`` 验证的关键模块（检出"包能导入但子模块损坏"）
#:
#: ⚠️ 只 import 顶层包名**不足以**发现"残缺包"（本次 openpyxl 就是
#: ``openpyxl/__init__.py`` 与 ``openpyxl/pivot/table.py`` 丢失）——
#: 因此必须逐个 import **深路径子模块**，把关键代码路径踩实。
IMPORT_CHECKS: tuple[str, ...] = (
    # PySide6（GUI 框架）
    "PySide6.QtCore",
    "PySide6.QtWidgets",
    "PySide6.QtGui",
    # OCR 链路
    "rapidocr",
    "onnxruntime",
    "numpy",
    "cv2",
    "PIL.Image",
    # 规则解析
    "yaml",
    "omegaconf",
    # rapidocr 传递依赖
    "pyclipper",
    "shapely",
    "requests",
    "certifi",
    "charset_normalizer",
    "antlr4",
    "flatbuffers",
    "six",
    "tqdm",
    "colorlog",
    # Excel 读写（含本次丢失的关键子模块 openpyxl.pivot.table）
    "openpyxl",
    "openpyxl.workbook",
    "openpyxl.pivot.table",
)

#: 正常的、无 ``__init__.py`` 但合法的目录名模式（不报空壳）
_NORMAL_DIR_PATTERNS: tuple[str, ...] = (
    "*.libs",
    "*.lib",
    "*.data",
    "*.dist-info",
    "*.egg-info",
    "*__pycache__",
    "*.dylibs",
)


@dataclass
class ShellPackage:
    """检测到的损坏包（空壳包 / 残缺包）。

    Attributes:
        name: 目录名。
        path: 目录绝对路径。
        kind: 损坏类型 —— ``"完全空目录"`` 或 ``"残缺包"``。
        file_count: 目录内（一层）文件数，用于区分两类损坏。
    """

    name: str
    path: str
    kind: str = "完全空目录"
    file_count: int = 0


@dataclass
class CheckReport:
    """体检报告。"""

    site_packages: str = ""
    issues: list[ShellPackage] = field(default_factory=list)
    import_failures: list[tuple[str, str]] = field(default_factory=list)
    version_warnings: list[str] = field(default_factory=list)
    ok_versions: list[str] = field(default_factory=list)
    #: 打包态下空壳包扫描被跳过的原因（非空表示"该节不适用"，**不计入 fatal**）
    scan_skipped_reason: str = ""

    @property
    def fatal(self) -> bool:
        """是否存在致命问题（空壳/残缺包 / 关键依赖 import 失败）。"""
        return bool(self.issues) or bool(self.import_failures)

    def render(self) -> str:
        """渲染人类可读报告。"""
        lines: list[str] = []
        lines.append("=" * 68)
        lines.append("环境体检报告 · tools/check_env.py（R11 防线）")
        lines.append("=" * 68)
        lines.append(f"site-packages: {self.site_packages}")
        lines.append(f"Python: {sys.version.split()[0]}  ({sys.executable})")
        lines.append(f"运行形态: {'PyInstaller 打包态（exe）' if is_frozen() else '源码/开发态'}")
        lines.append("")

        lines.append(f"[1/3] 关键依赖版本核对（{len(self.ok_versions)} 项通过）")
        for item in self.ok_versions:
            lines.append(f"      OK   {item}")
        for warn in self.version_warnings:
            lines.append(f"      WARN {warn}")
        lines.append("")

        lines.append(f"[2/3] 关键模块 import 验证（{len(IMPORT_CHECKS)} 项）")
        if self.import_failures:
            for module, err in self.import_failures:
                lines.append(f"      FAIL {module}: {err}")
        else:
            lines.append("      OK   全部关键模块 import 成功")
        lines.append("")

        lines.append("[3/3] 空壳包检测（无 __init__.py 且无 dist-info）")
        if self.scan_skipped_reason:
            # 打包态：该节**不适用**，必须明说而不是报警（否则全是误报）
            lines.append(f"       SKIP 本节不适用：{self.scan_skipped_reason}")
        elif self.issues:
            empty_count = sum(1 for pkg in self.issues if pkg.kind == "完全空目录")
            broken_count = len(self.issues) - empty_count
            lines.append(
                f"      FAIL 发现 {len(self.issues)} 个损坏包"
                f"（完全空目录 {empty_count} + 残缺包 {broken_count}）："
            )
            for pkg in self.issues:
                detail = f"[{pkg.kind}]"
                if pkg.kind == "残缺包":
                    detail += f"（目录内 {pkg.file_count} 个文件，但缺 __init__.py/dist-info）"
                lines.append(f"           - {pkg.name}  {detail}  →  {pkg.path}")
            lines.append("")
            lines.append("      处置：删除上述损坏目录后，执行")
            lines.append("            pip install --ignore-installed --no-cache-dir <包名>")
            lines.append("      并务必使用**全新干净 venv** 打包（架构设计 13.9）。")
        else:
            lines.append("      OK   未发现空壳包/残缺包")
        lines.append("")
        lines.append("-" * 68)
        if self.fatal:
            lines.append("体检结论：不通过 ✗  禁止打包（exit 1）")
        elif self.version_warnings:
            lines.append("体检结论：通过（有警告）⚠  建议核对版本（exit 2）")
        elif is_frozen():
            lines.append("体检结论：通过 ✓  exe 自检正常（exit 0）")
        else:
            lines.append("体检结论：通过 ✓  可继续打包（exit 0）")
        lines.append("=" * 68)
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  检测逻辑
# ══════════════════════════════════════════════════════════════════


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包态。"""
    return bool(getattr(sys, "frozen", False))


def find_site_packages(explicit: str | None = None) -> Path:
    """定位 ``site-packages`` 目录。

    Args:
        explicit: 显式指定目录（主要供测试注入伪造目录）。

    Returns:
        ``site-packages`` 路径；找不到时返回一个不存在的路径。
    """
    if explicit:
        return Path(explicit)

    # 打包态（PyInstaller）：onefile 会把全部依赖**平铺解到** sys._MEIPASS，
    # 该目录在语义上等价于 site-packages；directory 形式则在 _internal/。
    # 必须优先判断 —— 否则会顺着 sys.path 找到"宿主机的" site-packages，
    # 从而体检出一堆与本次交付无关的结论。
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass)
        if candidate.is_dir():
            return candidate

    for finder in sys.path:
        candidate = Path(finder)
        if candidate.is_dir() and candidate.name in ("site-packages", "dist-packages"):
            return candidate

    # 兜底：用 sysconfig 推定
    try:
        import sysconfig

        purelib = sysconfig.get_paths().get("purelib")
        if purelib:
            return Path(purelib)
    except Exception:  # noqa: BLE001
        pass

    return Path(sys.prefix) / "Lib" / "site-packages"


def _is_normal_dir(name: str) -> bool:
    """判断目录名是否属于"正常但无 ``__init__.py``"的类型。"""
    import fnmatch

    for pattern in _NORMAL_DIR_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True

    # 以 "libs" / "data" 结尾
    lowered = name.lower()
    if lowered.endswith((".libs", ".lib", ".data", ".dylibs")):
        return True

    # 纯 "__pycache__" 或点开头（隐藏目录）
    if name.startswith(".") or name == "__pycache__":
        return True

    return False


def _has_dist_info(site_packages: Path, pkg_name: str) -> bool:
    """判断包是否有对应的 ``dist-info`` / ``egg-info`` 元数据目录。"""
    candidates = [
        site_packages / f"{pkg_name}-",
    ]
    # glob 方式匹配（处理 "numpy-2.1.3.dist-info" 这类带版本号的目录）
    try:
        for entry in site_packages.iterdir():
            if not entry.is_dir():
                continue
            lowered = entry.name.lower()
            if not (lowered.endswith(".dist-info") or lowered.endswith(".egg-info")):
                continue
            stem = entry.name
            for suffix in (".dist-info", ".egg-info"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            # 包名归一化比较：去掉版本号（连字符后紧跟数字）
            normalized = stem.lower().replace("_", "-")
            target = pkg_name.lower().replace("_", "-")
            if normalized == target or normalized.split("-")[0] == target:
                return True
    except OSError:
        return False

    for candidate in candidates:
        if str(candidate) != str(site_packages):
            try:
                if list(site_packages.glob(candidate.name + "*")):
                    return True
            except OSError:
                pass

    return False


def _dist_owned_top_dirs(site_packages: Path) -> set[str]:
    """收集「被某个已安装发行版声明拥有」的顶层目录名集合（小写）。

    读取所有 ``*.dist-info/RECORD``（``*.egg-info`` 退回 ``SOURCES.txt``），
    取每条记录路径的**首段目录名**。

    **为什么需要它**：识别 **PEP 420 命名空间包**。这类目录
    **按设计就没有 ``__init__.py``**，且其内容由**另一个发行版**提供 ——
    典型是 ``google/``（由 ``protobuf`` 发行版提供 ``google/protobuf/`` 与
    ``google/_upb/``）。仅靠 :func:`_has_dist_info` 的「目录名 == 发行版名」
    判据无法命中，会把**完全正常的命名空间包误报为「残缺包」**，
    进而让 ``build_exe.ps1`` **在健康的干净环境上拒绝打包**。

    Args:
        site_packages: ``site-packages`` 目录。

    Returns:
        顶层目录名（小写）集合；读取失败时返回空集合（退化为原判据，不误放行）。
    """
    owned: set[str] = set()
    try:
        entries = list(site_packages.iterdir())
    except OSError:
        return owned

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue

        lowered = entry.name.lower()
        if lowered.endswith(".dist-info"):
            record = entry / "RECORD"
            is_csv = True
        elif lowered.endswith(".egg-info"):
            record = entry / "SOURCES.txt"
            is_csv = False
        else:
            continue

        try:
            if not record.is_file():
                continue
            with open(record, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    path = raw.split(",", 1)[0].strip() if is_csv else raw.strip()
                    if not path or path.startswith(".."):
                        continue
                    path = path.replace("\\", "/").lstrip("/")
                    top = path.split("/", 1)[0]
                    if not top:
                        continue
                    low = top.lower()
                    if low.endswith((".dist-info", ".egg-info")):
                        continue
                    owned.add(low)
        except OSError:
            continue

    return owned


@lru_cache(maxsize=8)
def _dist_owned_top_dirs_cached(site_packages_str: str) -> frozenset[str]:
    """对 :func:`_dist_owned_top_dirs` 做缓存（按 ``site-packages`` 路径）。

    仅在真的出现「候选损坏目录」时才被调用，正常环境下零开销。

    Args:
        site_packages_str: ``site-packages`` 路径字符串。

    Returns:
        顶层目录名（小写）不可变集合。
    """
    return frozenset(_dist_owned_top_dirs(Path(site_packages_str)))


def _classify_damaged_package(
    site_packages: Path, entry: Path
) -> ShellPackage | None:
    """判断单个目录是否为损坏包（空壳 / 残缺），是则返回 :class:`ShellPackage`。

    判定链路（命中任一"正常特征"即放行）：

    1. 目录名属于 ``*.libs`` / ``*.data`` / ``__pycache__`` 等正常模式 → 放行；
    2. 含 ``__init__.py`` → 正常包，放行；
    3. 含 ``py.typed`` → 类型存根包，放行；
    4. 有同名 ``dist-info`` / ``egg-info`` 元数据 → 正常（含命名空间包），放行；
    5. **被其它发行版的 ``RECORD`` 声明拥有** → PEP 420 命名空间包，放行
       （如 ``google/`` 由 ``protobuf`` 提供；无本步会把健康环境误判为残缺包）；
    6. ``*.libs`` / ``*.data`` 等经步骤 1 已放行，不会误伤二进制目录。

    以上皆不满足即视为**损坏目录**，再分两类：

    * 目录内**无任何文件且无子目录**（``files == [] and subdirs == []``）
      → **完全空目录**（pip 未落盘即中断）；
    * 其余（有文件或子目录，却缺 ``__init__.py`` / dist-info）
      → **残缺包**（关键文件丢失）。

    ⚠️ **重要**：一个包目录若**含子目录**（说明它本是"包"结构：有
    ``submodule/`` 子包），却缺 ``__init__.py``，无论其内是否残留零散
    ``.py`` 文件，都是**残缺包** —— 本次 ``openpyxl`` 就是这一类
    （``openpyxl/`` 下有 ``pivot/`` 子包，却丢了 ``__init__.py``）。
    因此**不能**用"顶层有 .py 文件"作为放行条件（那会漏检残缺包）。

    注：第 5 步**不会**放行真正的残缺包 —— 例如 ``openpyxl`` 安装中断时
    ``openpyxl-*.dist-info`` 一并缺失，没有任何 ``RECORD`` 会声明拥有
    ``openpyxl/``，故仍被正确判为残缺包（R11 原病例的检出能力不受影响）。

    Args:
        site_packages: 所属 ``site-packages``（用于查 dist-info）。
        entry: 待判定的子目录。

    Returns:
        命中则返回 :class:`ShellPackage`，否则 ``None``。
    """
    if not entry.is_dir():
        return None

    name = entry.name
    # 1. 正常的二进制/元数据/缓存目录（含 numpy.libs / shapely.libs）
    if _is_normal_dir(name):
        return None
    # 2. 正常包
    if (entry / "__init__.py").is_file():
        return None
    # 3. 类型存根
    if (entry / "py.typed").is_file():
        return None
    # 4. 有 dist-info/egg-info → 正常（含命名空间包）
    if _has_dist_info(site_packages, name):
        return None
    # 5. 由其它发行版的 RECORD 声明拥有 → PEP 420 命名空间包 → 正常
    if name.lower() in _dist_owned_top_dirs_cached(str(site_packages)):
        return None

    # 5. 判定损坏类型
    try:
        children = list(entry.iterdir())
    except OSError:
        children = []
    file_count = sum(1 for c in children if c.is_file())
    subdir_count = sum(1 for c in children if c.is_dir())

    # 完全空目录：无文件、无子目录
    if file_count == 0 and subdir_count == 0:
        return ShellPackage(
            name=name,
            path=str(entry),
            kind="完全空目录",
            file_count=0,
        )

    # 残缺包：有内容，却缺 __init__.py / dist-info
    return ShellPackage(
        name=name,
        path=str(entry),
        kind="残缺包",
        file_count=file_count,
    )


def detect_shell_packages(site_packages: Path) -> list[ShellPackage]:
    """遍历 ``site-packages`` 找出疑似损坏包（空壳包 + 残缺包）。

    判定条件：目录存在，且 **既无 ``__init__.py`` 也无 ``py.typed``，
    又无对应 dist-info/egg-info**，也不是正常的二进制/元数据目录。
    在此前提下：目录内无文件 → 「完全空目录」；有文件 → 「**残缺包**」。

    Args:
        site_packages: 待扫描目录。

    Returns:
        损坏包列表（含 ``kind`` 字段区分两类）。
    """
    found: list[ShellPackage] = []
    if not site_packages.is_dir():
        return found

    try:
        entries = sorted(site_packages.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found

    for entry in entries:
        damaged = _classify_damaged_package(site_packages, entry)
        if damaged is not None:
            found.append(damaged)

    return found


def _version_of(module_name: str, dist_name: str = "") -> str:
    """获取已安装包/模块的版本。

    优先读模块的 ``__version__``；缺失时退回 ``importlib.metadata.version``
    （部分包如 ``rapidocr`` 不暴露 ``__version__``，只能从 dist-info 读）。

    Args:
        module_name: import 名（如 ``"rapidocr"``）。
        dist_name: PyPI 分发名（如 ``"rapidocr"``）；缺省用 ``module_name``。

    Returns:
        版本字符串；均取不到时返回空串。
    """
    try:
        module = importlib.import_module(module_name)
        version = str(getattr(module, "__version__", "") or "")
        if version:
            return version
    except Exception:  # noqa: BLE001 - import 失败不算致命，继续尝试 metadata
        pass

    lookup = dist_name or module_name
    try:
        import importlib.metadata as metadata

        return str(metadata.version(lookup))
    except Exception:  # noqa: BLE001
        return ""


def _parse_version(text: str) -> tuple[int, ...]:
    """把版本字符串解析为可比较的数字元组（非数字段截断）。"""
    parts: list[int] = []
    for chunk in (text or "").replace("-", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_in_range(
    version: str, lower: str, upper: str
) -> tuple[bool, str]:
    """判定版本是否落在 ``[lower, upper)`` 区间内。

    Args:
        version: 实际版本。
        lower: 下界（含）；空串表示不检查。
        upper: 上界（不含）；空串表示不检查。

    Returns:
        ``(是否通过, 说明)``。
    """
    if not version:
        return False, "无法获取版本号"

    actual = _parse_version(version)
    if not actual:
        return True, f"{version}（版本号格式非标准，跳过范围检查）"

    if lower:
        if actual < _parse_version(lower):
            return False, f"{version} 低于要求下界 {lower}"
    if upper:
        if actual >= _parse_version(upper):
            return False, f"{version} 达到/超过上界 {upper}"

    return True, version


def check_versions(report: CheckReport) -> None:
    """核对关键依赖版本（写入 report）。"""
    for pkg_name, import_name, lower, upper, desc in CRITICAL_DEPS:
        version = _version_of(import_name, dist_name=pkg_name)
        if not version:
            # 版本拿不到不视为致命（有些包不暴露 __version__ 也无 dist-info），仅提示
            report.version_warnings.append(f"{pkg_name}: 未安装或无法获取版本（{desc}）")
            continue
        ok, note = _version_in_range(version, lower, upper)
        if ok:
            report.ok_versions.append(f"{pkg_name} {note}  ({desc})")
        else:
            report.version_warnings.append(f"{pkg_name}: {note}（{desc}）")


def check_imports(report: CheckReport) -> None:
    """逐个 import 关键模块（写入 report）。"""
    for module_name in IMPORT_CHECKS:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 任何 import 异常都要捕获
            report.import_failures.append((module_name, f"{type(exc).__name__}: {exc}"))


def run_check(site_packages: str | None = None, *, skip_imports: bool = False) -> CheckReport:
    """执行完整环境体检。

    Args:
        site_packages: 显式指定 ``site-packages`` 目录（供测试）。
        skip_imports: 跳过 import 验证（仅做空壳包检测，供快速自测）。

    Returns:
        :class:`CheckReport`。

    Note:
        **打包态（``sys.frozen``）下第 3 节「空壳包检测」不适用，会被跳过**：
        PyInstaller 把纯 Python 模块编译进 PYZ 归档，``_MEIPASS`` 里只落地
        二进制扩展与数据文件 —— 于是 ``PySide6/``（只有 ``.pyd``/``.dll``）、
        ``numpy/``（只有 ``.libs``）、``rules/``、``ui/`` 这些**正常的**目录
        全都命中「有文件但无 ``__init__.py``/``dist-info``」的残缺包判据，
        产生 13 条纯误报并把 exe 的 ``--check-env`` 打成 exit 1。

        打包态下真正有意义的是第 1、2 节（版本核对 + 逐包 import 验证），
        它们能检出「exe 解包损坏 / DLL 缺失 / 子模块没打进来」这类真问题。
    """
    report = CheckReport()
    sp = find_site_packages(site_packages)
    report.site_packages = str(sp)

    if not skip_imports:
        check_versions(report)
        check_imports(report)

    if is_frozen() and site_packages is None:
        report.scan_skipped_reason = (
            "当前运行在 PyInstaller 打包态：纯 Python 模块位于 PYZ 归档内，"
            "_MEIPASS 只落地二进制与数据文件，因此「无 __init__.py」属正常，"
            "该判据不适用（第 1、2 节仍有效）"
        )
    else:
        report.issues = detect_shell_packages(sp)
    return report


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表（缺省用 ``sys.argv[1:]``）。

    Returns:
        退出码：0 通过 / 1 致命 / 2 仅警告。
    """
    parser = argparse.ArgumentParser(
        prog="check_env.py",
        description="报关校验工具 · 打包前环境体检（R11 空壳包防线）",
    )
    parser.add_argument(
        "--scan-dir",
        dest="scan_dir",
        default=None,
        help="指定待扫描的 site-packages 目录（自测/伪造目录用）",
    )
    parser.add_argument(
        "--skip-imports",
        dest="skip_imports",
        action="store_true",
        help="跳过 import 与版本核对，仅做空壳包检测（快速自测用）",
    )
    parser.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help="仅在失败时输出（供 build_exe.ps1 调用）",
    )
    args = parser.parse_args(argv)

    report = run_check(args.scan_dir, skip_imports=args.skip_imports)

    if not args.quiet or report.fatal or report.version_warnings:
        print(report.render())

    if report.fatal:
        return 1
    if report.version_warnings:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
