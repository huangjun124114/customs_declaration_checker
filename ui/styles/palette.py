"""调色板 + QSS 加载（ui.styles.palette）。

集中定义：

  * 四类判定的展示色（徽标 / 卡片 / 结果表着色，与 SOP 1.3 一一对应）；
  * **断点红** ``BREAKPOINT_RED``（架构 12.A：仅「未完成断点」提示文案使用红色，
    非匹配断点只写 INFO 日志、绝不变红）；
  * 加载 ``app.qss`` 为整窗样式表。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.constants import (
    VERDICT_FAIL,
    VERDICT_NO_IMAGE,
    VERDICT_NO_MARK,
    VERDICT_PASS,
)

__all__ = [
    "BREAKPOINT_RED",
    "VERDICT_COLORS",
    "Palette",
    "load_stylesheet",
]

# ── 断点红（架构 12.A 指定，逐字不可改）──
BREAKPOINT_RED: str = "#D32F2F"

# ── 四类判定展示色 ──
_COLOR_PASS = "#2E7D32"      # 绿：校验合格
_COLOR_FAIL = "#C62828"      # 红：校验异常
_COLOR_NO_MARK = "#EF6C00"   # 橙：缺图内标识，人工复核
_COLOR_NO_IMAGE = "#1565C0"  # 蓝：缺图，人工复核

VERDICT_COLORS: dict[str, str] = {
    VERDICT_PASS: _COLOR_PASS,
    VERDICT_FAIL: _COLOR_FAIL,
    VERDICT_NO_MARK: _COLOR_NO_MARK,
    VERDICT_NO_IMAGE: _COLOR_NO_IMAGE,
}

_QSS_PATH: Path = Path(__file__).resolve().parent / "app.qss"


class Palette:
    """常用中性色常量（供内联样式 / 徽标底色复用）。"""

    BG = "#F5F6F8"
    CARD_BG = "#FFFFFF"
    BORDER = "#DCDFE6"
    TEXT = "#303133"
    TEXT_WEAK = "#909399"
    ACCENT = "#1565C0"
    DANGER = BREAKPOINT_RED
    OK = "#2E7D32"
    WARN = "#EF6C00"

    @staticmethod
    def verdict_color(verdict: str) -> str:
        """返回给定判定字符串对应的展示色（缺省中性色）。"""
        return VERDICT_COLORS.get(verdict, Palette.TEXT_WEAK)


def load_stylesheet(path: Path | None = None) -> str:
    """读取 QSS 文本（供 ``QApplication.setStyleSheet``）。

    Args:
        path: QSS 文件路径；缺省为同目录 ``app.qss``。

    Returns:
        QSS 文本；文件缺失时返回空串（不抛异常，保证 UI 仍可运行）。
    """
    target = path if path is not None else _QSS_PATH
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def apply_stylesheet(app: QApplication | None = None, path: Path | None = None) -> None:
    """把 ``app.qss`` 应用到 ``QApplication``（缺省取当前实例）。"""
    target_app = app if app is not None else QApplication.instance()
    if target_app is None:
        return
    qss = load_stylesheet(path)
    if qss:
        target_app.setStyleSheet(qss)
