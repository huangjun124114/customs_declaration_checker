"""资源定位（infra.resources，对应架构设计 9.7）。

统一解析「随程序分发的只读资源」（规则 YAML、QSS 样式、OCR 模型等）路径：
  * 开发态：资源位于工程根目录下的相对路径；
  * 打包态（PyInstaller）：资源位于 ``sys._MEIPASS`` 临时解包目录。

**注意**：本模块只解析**内置只读资源**。用户外置覆盖（``%APPDATA%\\CustomsChecker\\``）
由 :class:`core.rule_repository.RuleRepository` 负责，属于"可写配置"，不由本模块处理。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "is_frozen",
    "bundle_root",
    "app_base_dir",
    "project_root",
    "resource_path",
    "user_config_dir",
    "user_rules_dir",
]


def is_frozen() -> bool:
    """判断当前是否运行于 PyInstaller 打包后的冻结环境。

    Returns:
        ``True`` 表示打包态（``sys._MEIPASS`` 存在且 ``sys.frozen`` 为真）。
    """
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """返回「内置资源根目录」。

    打包态为 ``sys._MEIPASS``；开发态为工程根目录（本文件的上上级）。

    Returns:
        资源根 :class:`pathlib.Path`。
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[arg-type]
    # infra/resources.py → 上两级 = 工程根
    return Path(__file__).resolve().parent.parent


def app_base_dir() -> Path:
    """返回「程序落点目录」—— 打包态为 **exe 所在目录**，开发态为工程根。

    ⚠️ **与 :func:`bundle_root` 的区别是本函数存在的全部理由**：

    ==================  ==============================  ==============================
    函数                打包态返回                       用途
    ==================  ==============================  ==============================
    ``bundle_root()``   ``sys._MEIPASS``（**临时**）      只读内置资源（YAML/QSS/模型）
    ``app_base_dir()``  **exe 所在目录**（持久）        承接必须持久化的「过程产出 / 成果产出」
    ==================  ==============================  ==============================

    单文件（onefile）打包态下 ``sys._MEIPASS`` 指向
    ``%TEMP%\\_MEIxxxxxx``，**进程退出即被删除**。若把运行日志、断点写在那里，
    就会「用户机器上永远找不到日志」——违反「**可追溯**」质量红线。

    Returns:
        程序落点目录 :class:`pathlib.Path`。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """返回工程根目录（开发态）；打包态同样返回 ``_MEIPASS``。"""
    return bundle_root()


def resource_path(*parts: str) -> Path:
    """解析内置资源路径。

    Args:
        *parts: 相对资源根的路径片段，例如 ``"rules"``、``"fields_blacklist.yaml"``。

    Returns:
        绝对路径 :class:`pathlib.Path`（不保证存在，调用方自行校验）。

    Example:
        >>> resource_path("rules", "noise_signals.yaml").name
        'noise_signals.yaml'
    """
    return bundle_root().joinpath(*parts)


def user_config_dir() -> Path:
    """返回用户配置目录 ``%APPDATA%\\CustomsChecker``（不存在则创建）。

    Returns:
        用户配置目录 :class:`pathlib.Path`。
    """
    import os

    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:  # 非 Windows 或 APPDATA 缺失时的兜底
        base = Path.home() / ".config"
    target = base / "CustomsChecker"
    target.mkdir(parents=True, exist_ok=True)
    return target


def user_rules_dir() -> Path:
    """返回用户外置规则目录 ``%APPDATA%\\CustomsChecker\\rules``（不存在则创建）。

    该目录下的同名 YAML **优先于**内置规则（架构设计 12.C.3 N3）。

    Returns:
        用户规则目录 :class:`pathlib.Path`。
    """
    target = user_config_dir() / "rules"
    target.mkdir(parents=True, exist_ok=True)
    return target
