"""冻结态冒烟 + 交付物核对（``tools/smoke_frozen.py``）。

用途：
  在 **``build_exe.ps1`` 跑完之后** 运行，把"这个包到底能不能交付"这件事
  从"肉眼看一眼"变成"可复现的一串断言 + 一页纸结论"。

  它回答五个问题：

  1. **onedir 规模**：文件数 / 总字节 / exe 字节 —— 与上一版对比可发现
     「依赖被漏收」（体积骤降）或「混进运行期产物」（exe 旁多了 ``报关申报要素校验/``）。
  2. **分发包 zip**：精确字节、sha256、``testzip`` 完整性、条目数、
     **根下 exe 是否就位**（解压后双击能不能用，全看这一条）。
  3. **冻结态冒烟**：``--self-test`` / ``--check-env`` 的**退出码 + 耗时**。
     ⚠️ 打包态**绝不可抛异常**：窗口程序的 ``ImportError`` 会弹**模态框**，
     无人值守时永久挂起 —— 所以这里**必须**带 ``timeout``，超时即判失败。
  4. **``_internal/rules/`` YAML 是否收全**：与源码 ``rules/`` 目录**逐个比对**
     （不只数个数）。漏 YAML 会静默失效（缺陷 F 教训）。
  5. **版本号四处一致**：``main.py`` / ``ui/main_window.py`` / ``pyproject.toml``
     / zip 文件名 —— v0.3.0 曾漏更 ``ui/main_window.py``，留下陈旧值。

退出码：
  * ``0`` —— 全部关键项通过（可交付）；
  * ``1`` —— 有**致命项**失败（zip 缺失/损坏、冒烟非 0、YAML 缺件、版本不一致）。

用法::

    python tools/smoke_frozen.py                 # 结论打屏
    python tools/smoke_frozen.py --out _smoke.txt  # 同时落盘（便于 Read 查看）
    python tools/smoke_frozen.py --no-run        # 跳过冒烟执行（只核对产物静态信息）

⚠️ 本脚本**会真实执行 exe**，因此会在 onedir 目录内产生运行目录
``报关申报要素校验/{result,logs}/``。该目录已被 ``.gitignore`` 覆盖；
若只想留干净的包，冒烟后手工删掉即可（**不要**把它打进 zip）。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
SRC_RULES = REPO / "rules"

#: 冒烟子命令（对应 ``main.py`` 的 CLI 分支）。超时值给足：首次解压 + 依赖加载较慢。
SMOKE_FLAGS: tuple[str, ...] = ("--self-test", "--check-env")
SMOKE_TIMEOUT_S: int = 300


class Report:
    """收集结论行；``fail`` 标记致命项，决定退出码。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed: list[str] = []

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def fail(self, why: str) -> None:
        self.failed.append(why)
        self.add(f"  ✗ 致命：{why}")

    def warn(self, why: str) -> None:
        self.add(f"  ⚠️ 警告：{why}")

    def render(self) -> str:
        self.add()
        if self.failed:
            self.add(f"【结论】✗ 不通过（{len(self.failed)} 项致命）")
            for i, why in enumerate(self.failed, 1):
                self.add(f"  {i}. {why}")
        else:
            self.add("【结论】✓ 全部关键项通过，产物可交付")
        return "\n".join(self.lines)


# ─────────────────────────────────────────────────────────────────────
# 定位：不写死版本号与包名，全部从工程现状推导（换版本不用改脚本）
# ─────────────────────────────────────────────────────────────────────


def read_app_version() -> str:
    """从 ``main.py`` 读取 ``APP_VERSION``（唯一权威源）。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("APP_VERSION"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "?"


def find_onedir(app_name: str) -> Path | None:
    """``dist/<app_name>``；找不到则在 ``dist/`` 下找含 exe 的一层目录。"""
    cand = DIST / app_name
    if (cand / f"{app_name}.exe").is_file():
        return cand
    if not DIST.is_dir():
        return None
    for child in sorted(DIST.iterdir()):
        if child.is_dir() and (child / f"{child.name}.exe").is_file():
            return child
    return None


def find_zip() -> Path | None:
    """取 ``dist/`` 下**最新修改**的 zip（同名多次打包时以后者为准）。"""
    if not DIST.is_dir():
        return None
    zips = [p for p in DIST.glob("*.zip") if p.is_file()]
    return max(zips, key=lambda p: p.stat().st_mtime) if zips else None


def human(n: int) -> str:
    return f"{n:,} 字节 ({n / 1024 / 1024:.1f} MB)"


# ─────────────────────────────────────────────────────────────────────
# 五项检查
# ─────────────────────────────────────────────────────────────────────


def check_onedir(rep: Report, app_dir: Path | None) -> None:
    rep.add("【1】onedir 产物")
    if app_dir is None:
        rep.fail("dist/ 下找不到 onedir 产物目录")
        rep.add()
        return
    files = [p for p in app_dir.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    exe = app_dir / f"{app_dir.name}.exe"
    rep.add(f"  目录：{app_dir}")
    rep.add(f"  文件数：{len(files)}")
    rep.add(f"  总字节：{human(total)}")
    rep.add(f"  exe   ：{human(exe.stat().st_size)}")
    if not exe.is_file():
        rep.fail(f"exe 不在预期位置：{exe}")
    # ⚠️ 冒烟会把运行目录写进 onedir 顶层 —— 打包后再冒烟的话，压缩前务必确认
    stray = app_dir / "报关申报要素校验"
    if stray.is_dir():
        rep.warn(f"onedir 内已有运行目录（{stray.name}/），压缩前确认是否要剔除")
    rep.add()


def check_zip(rep: Report, zip_path: Path | None, app_name: str) -> None:
    rep.add("【2】分发包 zip")
    if zip_path is None:
        rep.fail("dist/ 下找不到 zip 分发包")
        rep.add()
        return
    size = zip_path.stat().st_size
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    rep.add(f"  路径  ：{zip_path.name}")
    rep.add(f"  字节  ：{human(size)}")
    rep.add(f"  sha256：{digest}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
            roots = sorted({n.split("/")[0] for n in names if n.split("/")[0]})
            if bad is not None:
                rep.fail(f"zip 内容损坏（testzip 报 {bad}）")
            rep.add(f"  完整性：{'OK' if bad is None else f'损坏于 {bad}'}")
            rep.add(f"  条目数：{len(names)}")
            rep.add(f"  顶层  ：{roots}")
            # 解压后能否直接双击 —— 取决于根下 exe 这一条
            want = f"{app_name}/{app_name}.exe"
            if want not in names:
                rep.fail(f"zip 根下缺 exe（期望条目 {want}）")
            else:
                rep.add("  根下 exe：True")
    except zipfile.BadZipFile as exc:
        rep.fail(f"zip 无法打开：{exc}")
    rep.add()


def check_smoke(rep: Report, app_dir: Path | None, enabled: bool) -> dict[str, str]:
    """跑冒烟子命令，返回 ``{flag: stderr 全文}`` 供后续断言复用。

    ⚠️ 日志走 **stderr**（``logging`` 默认流），不要因为"stderr 有内容"就判失败 ——
    冻结态那行 ``… v0.3.1 启动`` 正是最有力的版本证据。
    """
    captured: dict[str, str] = {}
    rep.add("【3】冻结态冒烟")
    if not enabled:
        rep.add("  已跳过（--no-run）")
        rep.add()
        return captured
    exe = (app_dir / f"{app_dir.name}.exe") if app_dir else None
    if exe is None or not exe.is_file():
        rep.fail("无 exe 可冒烟")
        rep.add()
        return captured
    for flag in SMOKE_FLAGS:
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [str(exe), flag],
                cwd=str(exe.parent),
                capture_output=True,
                timeout=SMOKE_TIMEOUT_S,
            )
            code: int | str = proc.returncode
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired:
            # 超时 = 打包态弹了模态框或死循环，属最严重的一类
            code, err = f"TIMEOUT({SMOKE_TIMEOUT_S}s)", ""
        except OSError as exc:
            code, err = f"OSError({exc})", ""
        elapsed = time.perf_counter() - started
        captured[flag] = err
        rep.add(f"  {flag:<12s} exit={code}  耗时={elapsed:.2f}s")
        if err:
            # 只回显关键行：完整日志很长，这里挑「启动」与「日志落点」两条
            for line in err.splitlines():
                if "启动" in line or "日志落点" in line or "ERROR" in line or "Traceback" in line:
                    rep.add(f"      {line.strip()[:160]}")
        if code != 0:
            rep.fail(f"{flag} 退出码 {code}（应 0）")
    rep.add()
    return captured


def check_rules(rep: Report, app_dir: Path | None) -> None:
    rep.add("【4】规则 YAML 收全")
    if app_dir is None:
        rep.add("  跳过（无 onedir）")
        rep.add()
        return
    frozen = app_dir / "_internal" / "rules"
    src_set = {p.name for p in SRC_RULES.glob("*.yaml")} if SRC_RULES.is_dir() else set()
    frz_set = {p.name for p in frozen.glob("*.yaml")} if frozen.is_dir() else set()
    rep.add(f"  源码 rules/      ：{len(src_set)} 个")
    rep.add(f"  _internal/rules/ ：{len(frz_set)} 个")
    missing = sorted(src_set - frz_set)
    extra = sorted(frz_set - src_set)
    rep.add(f"  缺失：{missing or '无'}")
    if extra:
        rep.add(f"  多余（源码已删但包内残留）：{extra}")
    if missing:
        # 缺陷 F 教训：漏 YAML 时兜底分支若不等价会静默失效
        rep.fail(f"_internal/rules 缺 {len(missing)} 个 YAML：{missing}")
    elif not frz_set:
        rep.fail("_internal/rules/ 为空或不存在")
    rep.add()


def check_versions(rep: Report, zip_path: Path | None) -> str:
    rep.add("【5】版本号一致性")

    def grab(path: Path) -> str:
        if not path.is_file():
            return "(缺失)"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return "(未找到)"

    main_ver = grab(REPO / "main.py")
    ui_ver = grab(REPO / "ui" / "main_window.py")
    py_ver = "(未找到)"
    py_toml = REPO / "pyproject.toml"
    if py_toml.is_file():
        for line in py_toml.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                py_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    zip_ver = "?"
    if zip_path is not None:
        # 文件名形如 <名称>_v<版本>_<MMDD>.zip
        stem = zip_path.stem
        zip_ver = stem.split("_v", 1)[1].split("_", 1)[0] if "_v" in stem else "?"
    rep.add(f"  main.py            APP_VERSION = {main_ver}")
    rep.add(f"  ui/main_window.py  APP_VERSION = {ui_ver}")
    rep.add(f"  pyproject.toml     version     = {py_ver}")
    rep.add(f"  zip 文件名版本                   = {zip_ver}")
    if not (main_ver == ui_ver == py_ver == zip_ver):
        rep.fail(
            "版本号不一致（main/ui/pyproject/zip）"
            f" → {main_ver} / {ui_ver} / {py_ver} / {zip_ver}"
        )
    else:
        rep.add(f"  四者一致：True（{main_ver}）")
    rep.add()
    return main_ver


def check_frozen_version(rep: Report, smoke_err: dict[str, str], version: str) -> None:
    """用 ``--self-test`` 的运行日志确认 exe 真实跑的是哪个版本。

    ⚠️ **不要**改用「在 exe 字节里搜版本串」的办法：纯 Python 模块被 PyInstaller
    放进 **PYZ 归档**（压缩），``_MEIPASS`` 只落地二进制与数据 —— 搜不到是**正常**的，
    会造出稳定复现的假报警。运行日志才是权威证据。
    """
    rep.add("【6】冻结态实际运行版本（权威证据）")
    text = smoke_err.get("--self-test", "")
    if not text:
        rep.add("  跳过（未执行 --self-test）")
        return
    want = f"v{version} 启动"
    if want in text:
        rep.add(f"  运行日志含「{want}」：True")
    else:
        rep.fail(f"运行日志未见「{want}」—— 冻结态版本与源码不一致（见 --self-test 输出）")


# ─────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结态冒烟 + 交付物核对")
    parser.add_argument("--out", type=Path, default=None, help="把结论写入该文件（UTF-8）")
    parser.add_argument("--no-run", action="store_true", help="跳过 exe 冒烟执行")
    parser.add_argument("--name", default="报关申报要素校验工具", help="onedir/应用名")
    args = parser.parse_args(argv)

    version = read_app_version()
    app_dir = find_onedir(args.name)
    zip_path = find_zip()

    rep = Report()
    rep.add(f"冻结态交付核对 · 版本 {version} · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    rep.add("=" * 66)
    rep.add()

    check_onedir(rep, app_dir)
    check_zip(rep, zip_path, args.name)
    smoke_err = check_smoke(rep, app_dir, enabled=not args.no_run)
    check_rules(rep, app_dir)
    check_versions(rep, zip_path)
    check_frozen_version(rep, smoke_err, version)

    text = rep.render()
    sys.stdout.write(text + "\n")
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
