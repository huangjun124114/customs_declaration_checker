# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（customs_checker.spec）。

对应架构设计 R1 / B.3 / 13.9：
  * 强制收集 rapidocr 的**模型与配置**（`collect_all('rapidocr')`）；
  * 强制收集 onnxruntime 的**动态库**（`collect_all('onnxruntime')`）；
  * 注入 ``rules/*.yaml``（规则外置载体）与 ``ui/styles/app.qss``；
  * **排除** PySide6 用不到的重模块（WebEngine / Sql / Quick / Multimedia），
    控制体积在 200–350MB（Q5 已拍板全内置）；
  * 支持 ``--onedir`` 备选（见 build_exe.ps1 的 ``-Onedir`` 开关）。

⚠️ 打包前**必须**先通过 ``tools/check_env.py`` 环境体检（R11 防线）。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

# ── 数据文件：规则 YAML + 样式表 + 环境体检脚本 ────────────────────
datas = []
datas += [("rules", "rules")]
datas += [("ui/styles/app.qss", "ui/styles")]
# tools/check_env.py 必须随包 —— 它是 ``exe --check-env`` 的实现，
# 也是现场排查「exe 自身体检」的唯一手段（main._run_env_check 走 bundle_root() 查找）。
datas += [("tools/check_env.py", "tools")]

# ── rapidocr：模型 + 配置（R1 关键，漏收会 ModelNotFound）─────────
rapidocr_datas, rapidocr_binaries, rapidocr_hiddenimports = collect_all("rapidocr")
datas += rapidocr_datas
binaries = list(rapidocr_binaries)
hiddenimports = list(rapidocr_hiddenimports)

# ── onnxruntime：动态库（R1/B.3，显式 collect 避免漏 DLL）──────────
ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
datas += ort_datas
binaries += list(ort_binaries)
hiddenimports += list(ort_hiddenimports)

# ── rapidocr 传递依赖的显式子模块（避免动态 import 漏收）───────────
for pkg in ("omegaconf", "pyclipper", "shapely", "yaml"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# rapidocr 3.x 读取自身 config.yaml，显式再收一次保险
try:
    datas += collect_data_files("rapidocr", includes=["**/*.yaml", "**/*.yml"])
except Exception:
    pass

# ── 排除 PySide6 用不到的重模块（体积对策，R1）────────────────────
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtSql",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.Qt3DCore",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    # 开发态依赖不入 exe（Q8：exe 内不含测试入口）
    "pytest",
    "pytest_qt",
    "ruff",
    "_pytest",
    "IPython",
    "matplotlib",
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
]

# 兼容 PyInstaller ≥6 的兼容常量
try:
    from PyInstaller.utils.hooks import check_requirement  # noqa: F401
except Exception:
    pass


a = Analysis(  # noqa: F821 - PyInstaller 注入的全局名
    ["main.py"],
    pathex=[os.path.abspath(os.getcwd())],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

# ── 两种输出形态（由 build_exe.ps1 的 -Onedir 开关经环境变量驱动）──────
#   CUSTOMS_ONEDIR=1 → 目录形式（onefolder，启动快、体积外露，旧机器友好）
#   其它/未设置      → 单文件 exe（onefile，全内置免安装，体积约 200–350MB）
_ONEDIR = os.environ.get("CUSTOMS_ONEDIR", "0").strip() in {"1", "true", "True", "yes"}

_EXE_COMMON = dict(  # noqa: F821 - PyInstaller 注入的全局名
    name="报关申报要素校验工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 窗口程序，无控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if _ONEDIR:
    # 目录形式：EXE 只放脚本，数据/二进制交给 COLLECT 平铺到 _internal/
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_EXE_COMMON)  # noqa: F821
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="报关申报要素校验工具",
    )
else:
    # 单文件：全部内联进 exe（默认交付形态）
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        **_EXE_COMMON,
    )
