"""依赖安装脚本（tools/install_deps.py）。

用途：
  一条命令把「运行时依赖 + opencv 归属纠正 + 环境体检」串起来，
  避免手工漏掉 opencv-python-headless 的纠正步骤而打出一个带着双 Qt 的 exe。

为什么需要它（而不是直接 ``pip install -r requirements.txt``）：
  rapidocr 的元数据里写死 ``Requires-Dist: opencv_python>=4.5.1.48``，
  与我们真正要用的 ``opencv-python-headless`` **包名不同、内容同占 ``cv2/`` 目录**。
  pip 的 resolver 无法表达这种互斥，两者会同装并争抢 ``cv2/``：
  后落盘者胜出。若被非 headless 版覆盖，exe 内会混入它自带的 Qt DLL，
  与 PySide6 形成「双 Qt」→ 运行时崩溃，且**打包阶段完全看不出问题**。

执行的三步（对应 requirements.txt 顶部注释）::

    1) pip install --no-cache-dir -r requirements.txt
    2) pip uninstall -y opencv-python
    3) pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

退出码：
  * ``0`` —— 三步全部成功（若带 ``--check``，还需 check_env 通过）；
  * ``1`` —— 任一步失败。

用法::

    python tools/install_deps.py              # 仅运行时依赖
    python tools/install_deps.py --with-dev   # 额外装 pytest / ruff / pyinstaller
    python tools/install_deps.py --check      # 装完立刻跑 check_env.py
    python tools/install_deps.py --dry-run    # 只打印将执行的命令，不动环境
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# 工程根：本文件位于 <root>/tools/install_deps.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
CHECK_ENV = PROJECT_ROOT / "tools" / "check_env.py"

# 非 headless 版 opencv 必须摘掉（见模块 docstring）
CONFLICTING_DIST = "opencv-python"
HEADLESS_REQ = "opencv-python-headless>=4.10,<5.0"

# 开发/打包期依赖（与 pyproject.toml [project.optional-dependencies].dev 保持一致）
DEV_DEPS = [
    "pytest>=8.0,<9.0",
    "pytest-qt>=4.4",
    "ruff>=0.5",
    "pyinstaller>=6.0,<7.0",
    "pyinstaller-hooks-contrib>=2024.0",
]


def _run(args: list[str], *, dry_run: bool) -> int:
    """执行一条命令并返回退出码；``dry_run`` 时只打印不执行。"""
    printable = " ".join(args)
    print(f"  $ {printable}", flush=True)
    if dry_run:
        return 0
    proc = subprocess.run(args, check=False)  # noqa: S603 - 参数由本脚本固定拼装
    return proc.returncode


def _pip(*args: str) -> list[str]:
    """拼出 ``<当前解释器> -m pip <args>``，确保装进当前 venv。"""
    return [sys.executable, "-m", "pip", *args]


def install(*, with_dev: bool, dry_run: bool) -> int:
    print("=" * 68)
    print("报关申报要素自动校验工具 · 依赖安装")
    print("=" * 68)
    print(f"解释器 : {sys.executable}")
    print(f"依赖清单: {REQUIREMENTS}")
    print()

    if not REQUIREMENTS.is_file():
        print(f"[错误] 找不到 {REQUIREMENTS}", file=sys.stderr)
        return 1

    print("[1/3] 安装运行时依赖（含 --no-cache-dir，规避 R11 空壳包）")
    rc = _run(_pip("install", "--no-cache-dir", "-r", str(REQUIREMENTS)), dry_run=dry_run)
    if rc != 0:
        print("[错误] 运行时依赖安装失败", file=sys.stderr)
        return 1

    if with_dev:
        print("\n[附加] 安装开发/打包期依赖")
        rc = _run(_pip("install", "--no-cache-dir", *DEV_DEPS), dry_run=dry_run)
        if rc != 0:
            print("[错误] 开发依赖安装失败", file=sys.stderr)
            return 1

    # ── 第 2 步：摘掉 pip 被 rapidocr 强拉进来的非 headless opencv ──
    # 该发行版可能本来就不存在（例如已手工纠正过），uninstall 返回非 0 属正常，故容错处理。
    print(f"\n[2/3] 摘除冲突发行版 {CONFLICTING_DIST}（不存在则跳过）")
    _run(_pip("uninstall", "-y", CONFLICTING_DIST), dry_run=dry_run)

    # ── 第 3 步：强制重装 headless 版，确保 cv2/ 目录归属正确 ──
    # 第 2 步会连带删掉 cv2/ 里的文件，所以这一步不只是"确保存在"，而是必须真装。
    print("\n[3/3] 强制重装 opencv-python-headless（确权 cv2/ 目录）")
    rc = _run(
        _pip("install", "--no-cache-dir", "--force-reinstall", "--no-deps", HEADLESS_REQ),
        dry_run=dry_run,
    )
    if rc != 0:
        print("[错误] opencv-python-headless 重装失败", file=sys.stderr)
        return 1

    print("\n依赖安装完成 ✓")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="安装本项目依赖，并纠正 opencv 的 headless 归属",
    )
    parser.add_argument("--with-dev", action="store_true", help="额外安装 pytest/ruff/pyinstaller")
    parser.add_argument("--check", action="store_true", help="安装后立即运行 tools/check_env.py")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不实际执行")
    args = parser.parse_args(argv)

    rc = install(with_dev=args.with_dev, dry_run=args.dry_run)
    if rc != 0:
        return rc

    if args.check and not args.dry_run:
        print("\n" + "=" * 68)
        print("运行环境体检 tools/check_env.py")
        print("=" * 68)
        return _run([sys.executable, str(CHECK_ENV)], dry_run=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
