"""依赖声明一致性回归锁（tests/test_dependency_manifest.py）。

背景（真实交付缺陷 · 2026-09-16）：
  ``core/result_exporter.py`` 与 ``core/excel_probe.py`` 强依赖 ``openpyxl``，
  但 ``requirements.txt`` / ``pyproject.toml`` 都漏了声明。补齐之前，全新环境
  ``pip install -r requirements.txt`` 装完仍然缺 openpyxl → 打包出的 exe 一旦
  导出汇总表就 ImportError，而用户机没有 Python，无法现场补救 —— 属 R11 同族风险。

  本模块把「代码实际 import 的第三方包」与「依赖清单声明的发行版」锁在一起：
  今后任何新增第三方 import 而忘记登记，都会在这里失败，而不是在客户现场失败。

覆盖三类断言：
  1. **正向锁**：源码里出现的每个第三方顶层模块，都能在依赖清单里找到对应发行版；
  2. **映射表完整性**：新增未登记的第三方 import 会因缺映射而失败（不允许静默跳过）；
  3. **交叉一致**：requirements.txt 与 pyproject.toml 的发行版集合一致，
     且 tools/check_env.py 的 CRITICAL_DEPS 全部被声明（体检表 ↔ 安装清单不脱节）。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
CHECK_ENV_PATH = PROJECT_ROOT / "tools" / "check_env.py"

#: 受检源码包（四层架构的业务代码）
SOURCE_PACKAGES = ("infra", "core", "app", "ui")

#: 顶层 import 名 → PyPI 发行版名。
#:
#: ⚠️ 新增第三方依赖时**必须**在此登记，否则 ``test_mapping_table_covers_all_imports`` 会失败。
#: 这是刻意设计：让「漏登记」成为红灯，而不是让检查悄悄放过。
IMPORT_TO_DIST: dict[str, str] = {
    "PySide6": "PySide6",
    "cv2": "opencv-python-headless",  # 必须 headless 版，避免与 PySide6 双 Qt 冲突
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "rapidocr": "rapidocr",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "onnxruntime": "onnxruntime",
    "omegaconf": "omegaconf",
    "pyclipper": "pyclipper",
    "shapely": "shapely",
}

#: 发行版名归一化后仍允许「只出现在注释里」的名字（不会作为真实依赖被断言）
_DIST_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize_dist(name: str) -> str:
    """按 PEP 503 归一化发行版名：小写 + 下划线/点统一成连字符。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_dist_names_from_requirements() -> set[str]:
    """解析 requirements.txt 的**有效行**（丢弃注释与空行），返回归一化发行版名集合。"""
    declared: set[str] = set()
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _DIST_NAME_RE.match(line)
        if match:
            declared.add(_normalize_dist(match.group(1)))
    return declared


def _declared_dist_names_from_pyproject() -> set[str]:
    """解析 pyproject.toml ``[project].dependencies``，返回归一化发行版名集合。"""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    declared: set[str] = set()
    for spec in deps:
        match = _DIST_NAME_RE.match(spec.strip())
        if match:
            declared.add(_normalize_dist(match.group(1)))
    return declared


def _scanned_third_party_imports() -> dict[str, set[str]]:
    """静态扫描业务代码，返回 ``{顶层模块名: {出现位置}}``（已剔除标准库与本地包）。"""
    stdlib = set(sys.stdlib_module_names)
    local = set(SOURCE_PACKAGES) | {"main", "tools", "tests", "conftest"}
    found: dict[str, set[str]] = {}

    for pkg in SOURCE_PACKAGES:
        for path in sorted((PROJECT_ROOT / pkg).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # 语法错误由其它用例负责报错，这里跳过
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # 相对 import（level > 0）属本地包
                    if node.level == 0 and node.module:
                        names = [node.module]
                for dotted in names:
                    top = dotted.split(".")[0]
                    if top in stdlib or top in local:
                        continue
                    found.setdefault(top, set()).add(str(path.relative_to(PROJECT_ROOT)))
    return found


def _load_check_env_module():
    """从文件路径加载 tools/check_env.py（它不是包，无法常规 import）。

    注意：``exec_module`` 之前必须先把模块登记进 ``sys.modules``，
    否则模块内的 dataclass / 自省逻辑会拿到 ``__dict__`` 为 ``None`` 的假模块。
    """
    spec = importlib.util.spec_from_file_location("_check_env_for_test", CHECK_ENV_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - 环境异常
        pytest.fail(f"无法加载 {CHECK_ENV_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


# ─────────────────────────── 1. 正面锁 ───────────────────────────


def test_every_third_party_import_is_declared() -> None:
    """代码里用到的第三方 top-level 模块，必须在依赖清单里有对应发行版。"""
    declared = _declared_dist_names_from_requirements()
    scanned = _scanned_third_party_imports()

    missing: list[str] = []
    for module in sorted(scanned):
        dist = IMPORT_TO_DIST.get(module)
        if dist is None:
            continue  # 由 test_mapping_table_covers_all_imports 负责报错
        if _normalize_dist(dist) not in declared:
            sample = sorted(scanned[module])[0]
            missing.append(f"{module} → 需声明发行版 {dist!r}（出现在 {sample}）")

    assert not missing, "requirements.txt 缺少以下依赖声明：\n  " + "\n  ".join(missing)


def test_mapping_table_covers_all_imports() -> None:
    """``IMPORT_TO_DIST`` 必须覆盖扫描到的全部第三方 import（防止漏登记被静默放过）。"""
    scanned = set(_scanned_third_party_imports())
    unmapped = sorted(scanned - set(IMPORT_TO_DIST))
    assert not unmapped, (
        "以下第三方 import 未登记到 IMPORT_TO_DIST，请补映射并同步依赖清单："
        f"\n  {unmapped}"
    )


def test_openpyxl_stays_declared() -> None:
    """回归锁：openpyxl 曾因漏声明导致 exe 内 ImportError，不得再被删掉。"""
    assert _normalize_dist("openpyxl") in _declared_dist_names_from_requirements()
    assert _normalize_dist("openpyxl") in _declared_dist_names_from_pyproject()


# ─────────────────────────── 2. 清单交叉一致 ───────────────────────────


def test_requirements_and_pyproject_agree() -> None:
    """requirements.txt 与 pyproject.toml 的发行版集合必须一致（防两处漂移）。"""
    from_req = _declared_dist_names_from_requirements()
    from_toml = _declared_dist_names_from_pyproject()
    only_req = sorted(from_req - from_toml)
    only_toml = sorted(from_toml - from_req)
    assert not only_req and not only_toml, (
        "两份依赖清单不一致：\n"
        f"  仅 requirements.txt 有：{only_req}\n"
        f"  仅 pyproject.toml 有：{only_toml}"
    )


def test_no_non_headless_opencv_declared() -> None:
    """禁止把非 headless 的 opencv-python 作为正式依赖声明（会与 PySide6 双 Qt 冲突）。"""
    active_lines: list[str] = []
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("-"):
            continue
        match = _DIST_NAME_RE.match(line)
        if match and _normalize_dist(match.group(1)) == "opencv-python":
            active_lines.append(raw_line.strip())
    assert not active_lines, (
        "requirements.txt 不得声明非 headless 的 opencv-python（应仅声明 opencv-python-headless）："
        f"\n  {active_lines}"
    )


# ─────────────────────────── 3. 体检表 ↔ 安装清单 ───────────────────────────


def test_check_env_critical_deps_are_all_declared() -> None:
    """``tools/check_env.py`` 逐项核对的发行版，必须全部出现在依赖清单里。

    否则会出现「体检表要求 X，但安装清单根本不装 X」的脱节 ——
    本次 flatbuffers / antlr4-python3-runtime 就是这样被体检抓出来的。
    """
    module = _load_check_env_module()
    declared = _declared_dist_names_from_requirements()

    critical = {_normalize_dist(dist) for dist, _import, _lo, _hi, _desc in module.CRITICAL_DEPS}
    missing = sorted(critical - declared)
    assert not missing, (
        "check_env 要核对的发行版未在 requirements.txt 声明："
        f"\n  {missing}"
    )
