"""原始 OCR 文本弹窗（ui.widgets.ocr_text_dialog，v0.3.0 需求 8）。

**为什么要独立弹窗**：v0.2.0 的复核卡里同时存在「查看原始 OCR 全文」（可折叠区）与
「当前图 OCR 文本」两个控件，**内容重复**。v0.3.0 只保留一个入口
（按钮「查看原始 OCR 文本」），点击后用**弹窗**展示。

**关键约束（需求 8 原文）**：**弹窗不要遮挡图片区域**。实现要点：

  * **非模态** + ``Qt.WindowType.Tool`` —— 不阻塞主界面，用户可边看图边对照文本；
  * **单例**（由调用方 :class:`ui.widgets.workbench_card.WorkbenchCard` 持有一个实例）
    —— 重复点击只置顶，不叠加窗口；
  * **默认定位在主窗口右侧外**（``x = 主窗口右边界 + 8``）；右侧空间不足时回退到
    **屏幕右缘**并尽量贴着图片区右侧外放置，绝**不覆盖**图片区（见
    :meth:`OcrTextDialog.show_beside`）；
  * 提供 ``◀ 上一张`` / ``下一张 ▶``，可不关弹窗连续翻阅本记录所有图的 OCR；
  * 提供「复制全文」。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["OcrTextDialog"]

#: 弹窗默认尺寸
_DEFAULT_WIDTH = 560
_DEFAULT_HEIGHT = 460
#: 与主窗口 / 图片区的横向间距
_GAP = 8


class OcrTextDialog(QDialog):
    """「原始 OCR 文本」**非模态**弹窗（单例由调用方持有）。

    Signals:
        previous_requested: 点击「◀ 上一张」。
        next_requested: 点击「下一张 ▶」。

    Args:
        parent: Qt 父控件（用于取其所属顶层窗口做定位）。
    """

    previous_requested = Signal()
    next_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造弹窗。"""
        super().__init__(parent)
        # 非模态 + Tool：不阻塞主界面、不占任务栏
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowTitle("原始 OCR 文本")
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        self._build_ui()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建弹窗布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self.title_label = QLabel("", self)
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀ 上一张", self)
        self.btn_next = QPushButton("下一张 ▶", self)
        self.btn_prev.clicked.connect(self.previous_requested.emit)
        self.btn_next.clicked.connect(self.next_requested.emit)
        nav.addWidget(self.btn_prev)
        nav.addWidget(self.btn_next)
        nav.addStretch(1)
        self.btn_copy = QPushButton("复制全文", self)
        self.btn_copy.clicked.connect(self._on_copy)
        nav.addWidget(self.btn_copy)
        outer.addLayout(nav)

        self.text_view = QPlainTextEdit(self)
        self.text_view.setReadOnly(True)
        self.text_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.text_view.font()
        font.setFamily("Consolas")
        self.text_view.setFont(font)
        outer.addWidget(self.text_view, 1)

        self.hint_label = QLabel("提示：本窗口为非模态，可边看图边对照。", self)
        self.hint_label.setObjectName("reviewHint")
        outer.addWidget(self.hint_label)

    # ─────────────────────── 内容 ───────────────────────

    def set_content(
        self,
        title: str,
        text: str,
        *,
        has_prev: bool = False,
        has_next: bool = False,
    ) -> None:
        """设置标题、正文与上一张/下一张可用性。

        Args:
            title: 标题（如 ``当前图 2 — C:/img/2.jpg``）。
            text: OCR 全文（散行）。
            has_prev: 是否可切上一张。
            has_next: 是否可切下一张。
        """
        self.title_label.setText(title)
        self.text_view.setPlainText(text)
        self.btn_prev.setEnabled(bool(has_prev))
        self.btn_next.setEnabled(bool(has_next))
        self.setWindowTitle(f"原始 OCR 文本 — {title}" if title else "原始 OCR 文本")

    def current_text(self) -> str:
        """返回当前正文（供测试）。"""
        return self.text_view.toPlainText()

    # ─────────────────────── 定位 ───────────────────────

    def show_beside(
        self,
        *,
        main_window: QWidget | None = None,
        avoid: QWidget | None = None,
    ) -> None:
        """**不遮挡图片区**地显示（需求 8 关键约束）。

        按**候选位优先级**择一，先满足「不与 ``avoid`` 重叠」，再尽量落在屏内：

          1. **主窗口右侧外**（``x = 主窗口.right() + 8``）—— 首选，图片区在窗口内
             时必然不遮；
          2. **图片区右侧外**（``x = 图片区.right() + 8``）；
          3. **图片区下方**（``y = 图片区.bottom() + 8``）；
          4. 主窗口下方；
          5. 屏幕左上角兜底。

        **优先级说明**：当「不遮图」与「完整落在屏内」冲突时，**前者优先**——
        弹窗宁可溢出屏幕边缘，也不覆盖图片区（用户的核心诉求是边看图边对照）。
        极端情况下（图片区占满屏幕）会压缩弹窗高度贴到图片区下方。

        Args:
            main_window: 主窗口；``None`` 时取本弹窗的顶层窗口。
            avoid: 不得被遮挡的控件（图片区）。
        """
        window = main_window if main_window is not None else self.window()
        screen = (
            QGuiApplication.screenAt(window.frameGeometry().center())
            if window is not None
            else None
        ) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()

        width = max(self.width(), _DEFAULT_WIDTH)
        height = max(self.height(), _DEFAULT_HEIGHT)
        anchor = avoid.geometry() if avoid is not None else None
        win_geo = window.geometry() if window is not None else None

        candidates: list[tuple[int, int]] = []
        if win_geo is not None:
            candidates.append((win_geo.right() + _GAP, win_geo.top() + 120))
        if anchor is not None:
            candidates.append((anchor.right() + _GAP, anchor.top()))
            candidates.append((anchor.left(), anchor.bottom() + _GAP))
        if win_geo is not None:
            candidates.append((win_geo.left() + _GAP, win_geo.bottom() + _GAP))
        candidates.append((area.left() + _GAP, area.top() + _GAP))

        target: QRect | None = None
        fallback: QRect | None = None
        for x, y in candidates:
            rect = QRect(int(x), int(y), width, height)
            if anchor is not None and rect.intersects(anchor):
                continue
            if fallback is None:
                fallback = rect
            if self._fits(area, rect):
                target = rect
                break
        rect = target or fallback
        if rect is None:  # pragma: no cover - 全部候选都遮图（屏幕小于图片区）
            rect = self._below_anchor(area, anchor, width, height)
        self.setGeometry(self._clamp(area, rect, anchor))
        self.show()
        self.raise_()
        self.activateWindow()
        if self.isMinimized():
            self.showNormal()

    @staticmethod
    def _fits(area: QRect, rect: QRect) -> bool:
        """矩形是否完整落在可用屏幕区内。"""
        return area.contains(rect)

    @staticmethod
    def _clamp(area: QRect, rect: QRect, anchor: QRect | None) -> QRect:
        """把矩形收进屏幕；若收进来反而遮住图片区，则保持原样（不遮图优先）。"""
        clamped = QRect(rect)
        clamped.moveLeft(
            min(max(clamped.left(), area.left() + _GAP), max(area.right() - clamped.width(), area.left()))
        )
        clamped.moveTop(
            min(max(clamped.top(), area.top() + _GAP), max(area.bottom() - clamped.height(), area.top()))
        )
        if anchor is not None and clamped.intersects(anchor) and not rect.intersects(anchor):
            return rect
        return clamped

    @staticmethod
    def _below_anchor(area: QRect, anchor: QRect | None, width: int, height: int) -> QRect:
        """极端兜底：贴到图片区下方，并把高度压到剩余空间内。"""
        if anchor is None:  # pragma: no cover - 无图片区时不会走到这里
            return QRect(area.left() + _GAP, area.top() + _GAP, width, height)
        y = anchor.bottom() + _GAP
        room = area.bottom() - y
        return QRect(anchor.left(), y, width, max(min(height, room), 160))

    def overlap_with(self, widget: QWidget) -> bool:
        """返回本弹窗是否与 ``widget`` 的矩形**重叠**（供测试断言"不遮图"）。"""
        return self.geometry().intersects(widget.geometry())

    # ─────────────────────── 内部 ───────────────────────

    def _on_copy(self) -> None:
        """复制全文到剪贴板。"""
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_view.toPlainText())
        self.hint_label.setText("已复制全文到剪贴板。")
