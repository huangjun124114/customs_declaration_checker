"""原始 OCR 文本弹窗（ui.widgets.ocr_text_dialog，v0.3.0 需求 8）。

**为什么要独立弹窗**：v0.2.0 的复核卡里同时存在「查看原始 OCR 全文」（可折叠区）与
「当前图 OCR 文本」两个控件，**内容重复**。v0.3.0 只保留一个入口
（按钮「查看原始 OCR 文本」），点击后用**弹窗**展示。

**关键约束**：① **不遮挡图片区域**；② 定位在**主窗口右侧、垂直居中**（**不置顶**）。
实现要点：

  * **非模态** + ``Qt.WindowType.Tool`` —— 不阻塞主界面，用户可边看图边对照文本；
  * **单例**（由调用方 :class:`ui.widgets.workbench_card.WorkbenchCard` 持有一个实例）
    —— 重复点击只置顶，不叠加窗口；
  * **右区自适应**：以「图片区右边界 → 主窗口右边界」为合法右区，弹窗**按需收窄**
    后**右对齐**，并**垂直居中于主窗口**（见 :meth:`OcrTextDialog.show_beside`）；
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
#: 弹窗最小可用尺寸（再小就没法读文本）
_MIN_WIDTH = 360
_MIN_HEIGHT = 240
#: 与主窗口 / 图片区的间距
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
        """在**主窗口右侧、垂直居中**显示（需求 8 + 现场反馈修正）。

        **定位规则（v0.3.1）**：

          1. 「右侧页面」= **图片区右边界 → 主窗口右边界**，并收敛进屏幕可用区；
          2. 宽度 = ``clamp(默认宽, 右侧可用宽度)``（下限 :data:`_MIN_WIDTH`）——
             即**按需收窄**而不是溢出屏幕，从而同时满足「在右侧」与「不遮图」；
          3. ``x`` **右对齐**（贴右侧边界），``y`` = 主窗口**垂直中心** → 弹窗垂直居中，
             **不再置顶**；
          4. 若右侧放不下（小屏 / 图片区几乎占满窗口）→ **退化为贴图片区下方**，
             优先保住「不遮图」；连下方也无空间时，才贴右侧并允许压住图片区
             （此时屏幕上不存在"既在右侧又不遮图"的合法位置）。

        ⚠️ **改动原因（实测反馈）**：旧实现把候选位依次试「主窗口右侧外 → 图片区右侧外 →
        图片区下方 → 屏幕左上角」，且要求候选位**完整落在屏内**才采纳 ——
        主窗口**最大化**时"右侧外"必然出屏，逐级退化后最终落到**屏幕左上角 (8,8)**，
        既置顶又压住图片区。现改为**先算合法右区、再右对齐垂直居中**，不做盲试。

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

        anchor = avoid.geometry() if avoid is not None else None
        win_geo = window.frameGeometry() if window is not None else None

        self.setGeometry(self.place(area, win_geo, anchor))
        self.show()
        self.raise_()
        self.activateWindow()
        if self.isMinimized():
            self.showNormal()

    @classmethod
    def place(
        cls,
        area: QRect,
        win_geo: QRect | None,
        anchor: QRect | None,
    ) -> QRect:
        """计算弹窗目标矩形（**纯函数**，与屏幕尺寸无关 → 可在任意分辨率下单测）。

        Args:
            area: 屏幕可用区。
            win_geo: 主窗口 frame 矩形；``None`` 表示无主窗口。
            anchor: 不得被遮挡的控件矩形（图片区）；``None`` 表示无约束。

        Returns:
            弹窗应落的目标矩形。
        """
        # ① 右侧可用区：图片区右边界（或窗口中线）→ 主窗口右边界 ∩ 屏幕
        if anchor is not None:
            left_bound = anchor.right() + _GAP
        elif win_geo is not None:
            left_bound = win_geo.center().x()
        else:
            left_bound = area.center().x()
        right_edge = min(
            (win_geo.right() if win_geo is not None else area.right()) - _GAP,
            area.right() - _GAP,
        )
        center_y = win_geo.center().y() if win_geo is not None else area.center().y()
        height = max(_MIN_HEIGHT, min(_DEFAULT_HEIGHT, area.height() - 2 * _GAP))

        # ② 右侧放得下 → 按需收窄 + **右对齐** + **垂直居中**
        if right_edge - left_bound >= _MIN_WIDTH:
            width = max(_MIN_WIDTH, min(_DEFAULT_WIDTH, right_edge - left_bound))
            return cls._clamp(
                area,
                QRect(
                    int(right_edge - width),
                    int(center_y - height // 2),
                    int(width),
                    int(height),
                ),
            )

        # ③ 右侧放不下（小屏 / 图片区几乎占满窗口）→ 贴图片区下方，优先保住「不遮图」
        below = cls._below_anchor(area, anchor, _DEFAULT_WIDTH, height)
        if below.height() >= _MIN_HEIGHT and not (
            anchor is not None and below.intersects(anchor)
        ):
            return cls._clamp(area, below)

        # ④ 上下左右皆无空间 → 默认尺寸贴右侧、垂直居中（允许压住图片区）
        return cls._clamp(
            area,
            QRect(
                int(right_edge - _DEFAULT_WIDTH),
                int(center_y - height // 2),
                _DEFAULT_WIDTH,
                int(height),
            ),
        )

    @staticmethod
    def _clamp(area: QRect, rect: QRect) -> QRect:
        """把矩形收进屏幕可用区（纯几何收敛，不做遮图判断）。"""
        clamped = QRect(rect)
        clamped.moveLeft(
            min(
                max(clamped.left(), area.left() + _GAP),
                max(area.right() - clamped.width(), area.left() + _GAP),
            )
        )
        clamped.moveTop(
            min(
                max(clamped.top(), area.top() + _GAP),
                max(area.bottom() - clamped.height(), area.top() + _GAP),
            )
        )
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
