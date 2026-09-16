"""可缩放图片查看器（ui.widgets.image_viewer）。

人工复核工作台「左图右证据」中**左侧的产品实拍图**查看控件：

  * 支持**缩放**（滚轮 / 按钮 / 适应窗口 / 1:1）；
  * 支持**拖拽平移**（放大后拖动查看细节）；
  * 加载失败 / 路径为空时给出**明确占位提示**（🔵 缺图时**不加载任何图片**，
    只显示「缺图」文案 + 提示用户「标记待补图 + 备注」，见 FR：🔵 无图预览）。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.styles.palette import Palette

__all__ = ["ImageViewer"]

_MIN_SCALE = 0.05
_MAX_SCALE = 12.0


class ImageViewer(QFrame):
    """可缩放 / 可平移的图片查看器。

    Signals:
        load_failed: 加载图片失败（携带路径与原因）。

    Args:
        parent: Qt 父控件。
    """

    load_failed = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造图片查看器。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._pixmap: QPixmap | None = None
        self._scale: float = 1.0
        self._current_path: str = ""
        self._build_ui()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建查看器布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("产品实拍图：", self))
        bar.addStretch(1)
        self.btn_zoom_out = QPushButton("－", self)
        self.btn_zoom_in = QPushButton("＋", self)
        self.btn_fit = QPushButton("适应窗口", self)
        self.btn_actual = QPushButton("1:1", self)
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.btn_fit.clicked.connect(self.fit_to_window)
        self.btn_actual.clicked.connect(self.reset_zoom)
        for btn in (self.btn_zoom_out, self.btn_zoom_in, self.btn_fit, self.btn_actual):
            bar.addWidget(btn)
        self.scale_label = QLabel("100%", self)
        self.scale_label.setMinimumWidth(56)
        self.scale_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.scale_label)
        outer.addLayout(bar)

        # 滚动区 + 图片标签
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label = QLabel("（未选择记录）", self)
        self.image_label.setObjectName("imageStage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(320, 260)
        self.image_label.setScaledContents(False)
        self.image_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.scroll.setWidget(self.image_label)
        outer.addWidget(self.scroll, 1)

        self.path_label = QLabel("", self)
        self.path_label.setObjectName("reviewHint")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self.path_label)

    # ─────────────────────── 公开 API ───────────────────────

    def show_message(self, text: str) -> None:
        """显示纯文本占位（无图 / 缺图场景，**不加载图片**）。"""
        self._pixmap = None
        self._current_path = ""
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(text)
        self.path_label.setText("")
        self.scale_label.setText("—")

    def load(self, image_path: str) -> bool:
        """加载图片（成功返回 True）。

        Args:
            image_path: 图片绝对路径。

        Returns:
            是否成功加载并显示。
        """
        path = (image_path or "").strip()
        if not path:
            self.show_message("（该记录无图片证据）")
            return False
        pix = QPixmap(path)
        if pix.isNull():
            self.show_message("⚠ 图片无法加载（文件缺失或格式不支持）")
            self.path_label.setText(f"路径：{path}")
            self.load_failed.emit(path, "无法加载图片")
            return False

        self._pixmap = pix
        self._current_path = path
        self.path_label.setText(f"路径：{path}　（{pix.width()}×{pix.height()}）")
        self._scale = 1.0
        self._render()
        # 首次加载自动适应窗口
        self.fit_to_window()
        return True

    def zoom_by(self, factor: float) -> None:
        """按倍数缩放（相对当前）。"""
        if self._pixmap is None:
            return
        self._scale = max(_MIN_SCALE, min(_MAX_SCALE, self._scale * float(factor)))
        self._render()

    def reset_zoom(self) -> None:
        """恢复 1:1。"""
        if self._pixmap is None:
            return
        self._scale = 1.0
        self._render()

    def fit_to_window(self) -> None:
        """缩放到适应可视区。"""
        if self._pixmap is None:
            return
        viewport = self.scroll.viewport().size()
        pw = max(1, self._pixmap.width())
        ph = max(1, self._pixmap.height())
        avail_w = max(1, viewport.width() - 8)
        avail_h = max(1, viewport.height() - 8)
        self._scale = max(_MIN_SCALE, min(_MAX_SCALE, min(avail_w / pw, avail_h / ph)))
        self._render()

    def current_scale(self) -> float:
        """返回当前缩放系数。"""
        return self._scale

    # ─────────────────────── 内部 ───────────────────────

    def _render(self) -> None:
        """按当前缩放渲染图片。"""
        if self._pixmap is None:
            return
        pw = max(1, int(self._pixmap.width() * self._scale))
        ph = max(1, int(self._pixmap.height() * self._scale))
        scaled = self._pixmap.scaled(
            pw,
            ph,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setText("")
        self.image_label.setPixmap(scaled)
        self.image_label.resize(scaled.size())
        self.scale_label.setText(f"{int(round(self._scale * 100))}%")

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """Ctrl + 滚轮缩放（普通滚轮交给滚动区）。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_by(1.1)
            elif delta < 0:
                self.zoom_by(1 / 1.1)
            event.accept()
            return
        super().wheelEvent(event)

    def clear(self) -> None:
        """清空并复位。"""
        self._pixmap = None
        self._current_path = ""
        self._scale = 1.0
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("（未选择记录）")
        self.path_label.setText("")
        self.scale_label.setText("—")

    def style_hint(self) -> Palette:  # pragma: no cover - 便捷访问
        """返回调色板（供外部统一取色）。"""
        return Palette
