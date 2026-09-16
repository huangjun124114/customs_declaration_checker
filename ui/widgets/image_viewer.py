"""可缩放图片查看器（ui.widgets.image_viewer）。

人工复核工作台「左图右证据」中**左侧的产品实拍图**查看控件：

  * 支持**缩放**（滚轮 / 按钮 / 适应窗口 / 1:1）；
  * 支持**拖拽平移**（放大后按住鼠标左键拖动查看细节）；
  * 加载失败 / 路径为空时给出**明确占位提示**（🔵 缺图时**不加载任何图片**，
    只显示「缺图」文案 + 提示用户「标记待补图 + 备注」，见 FR：🔵 无图预览）。

**v0.2.0 点 9.1 —— 拖动平移 bug 修复**（根因：布局自矛盾）：
原先 ``setWidgetResizable(True)``（强制 label 拉伸到视口大小）与
``_render()`` 里 ``image_label.resize(scaled.size())``（按缩放后尺寸调整 label）
**行为冲突** → 图片放大后超出视口却**不出现滚动条、也无法拖动**。修法：

  1. ``setWidgetResizable(False)``：label 保持自身尺寸，图片小于视口时由
     ``scroll.setAlignment(AlignCenter)`` 居中，大于视口时**按需出现滚动条**；
  2. 新增**鼠标左键拖拽平移**：``mousePressEvent`` / ``mouseMoveEvent`` /
     ``mouseReleaseEvent`` 直接操作 ``scroll.horizontalScrollBar()`` /
     ``verticalScrollBar()``，光标 ``OpenHandCursor`` → ``ClosedHandCursor``，
     松手复位；
  3. ``Ctrl + 滚轮`` 缩放**保留不动**（现状已有）。
  4. 抓手光标**仅在放大超出视口时**出现 —— 未超出时保持箭头，避免误导用户。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QResizeEvent, QWheelEvent
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
        #: 拖拽平移状态机（按下 → 移动 → 松手）
        self._dragging: bool = False
        self._drag_origin: QPoint = QPoint()
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
        # 点 9.1：关掉 WidgetResizable —— label 保持自身尺寸（= 缩放后图片尺寸），
        # 图片超出视口时滚动条才会出现，从而支持拖动平移。
        self.scroll.setWidgetResizable(False)
        # 图片小于视口时居中显示；大于视口时该对齐被忽略、按需出现滚动条。
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
        # 图片标签会吞掉鼠标事件，故安装事件过滤器把「按下/移动/松手」转发给
        # 查看器自身的拖拽处理器（仅在展示图片时拦截，文本可选择片段不受影响）。
        self.image_label.installEventFilter(self)
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
        self._dragging = False
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(text)
        self.path_label.setText("")
        self.scale_label.setText("—")
        self._apply_cursor()

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

    def current_path(self) -> str:
        """返回当前加载的图片路径（未加载时为空串）。"""
        return self._current_path

    def is_pannable(self) -> bool:
        """返回当前图片是否**已放大超出视口**（可平移）。

        Returns:
            ``True`` 表示存在可滚动的溢出区域（拖动平移才会生效）。
        """
        if self._pixmap is None:
            return False
        viewport = self.scroll.viewport().size()
        return (
            self.image_label.width() > viewport.width()
            or self.image_label.height() > viewport.height()
        )

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
        # 点 9.1：显式把 label 调整到缩放后尺寸 —— 配合 setWidgetResizable(False)，
        # 超出视口时滚动条随即出现（不再被 WidgetResizable 反向拉伸覆盖）。
        self.image_label.resize(scaled.size())
        self.scale_label.setText(f"{int(round(self._scale * 100))}%")
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        """按「是否可平移 / 是否正在拖动」刷新鼠标光标。

        抓手光标**仅在放大超出视口时**出现；未超出时保持箭头（避免用户困惑）。
        """
        if self._pixmap is None:
            target = Qt.CursorShape.ArrowCursor
        elif self._dragging:
            target = Qt.CursorShape.ClosedHandCursor
        elif self.is_pannable():
            target = Qt.CursorShape.OpenHandCursor
        else:
            target = Qt.CursorShape.ArrowCursor
        self.image_label.setCursor(target)
        self.scroll.viewport().setCursor(target)

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # noqa: N802 - Qt 覆写命名
        """把图片标签上的鼠标事件转发给拖拽处理器。

        仅在**正在展示图片**（``_pixmap is not None``）时拦截；显示纯文本占位时
        放行，保留「文本可选择」能力。
        """
        if obj is self.image_label and self._pixmap is not None:
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonPress:
                self.mousePressEvent(event)  # type: ignore[arg-type]
                return True
            if event_type == QEvent.Type.MouseMove:
                self.mouseMoveEvent(event)  # type: ignore[arg-type]
                return True
            if event_type == QEvent.Type.MouseButtonRelease:
                self.mouseReleaseEvent(event)  # type: ignore[arg-type]
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """左键按下：仅在图片超出视口时进入拖拽平移态。"""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._pixmap is not None
            and self.is_pannable()
        ):
            self._dragging = True
            self._drag_origin = event.position().toPoint()
            self._apply_cursor()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """拖拽中：按位移反向滚动，实现"抓住图片拖动"的手感。"""
        if self._dragging:
            position = event.position().toPoint()
            delta = position - self._drag_origin
            hbar = self.scroll.horizontalScrollBar()
            vbar = self.scroll.verticalScrollBar()
            hbar.setValue(hbar.value() - delta.x())
            vbar.setValue(vbar.value() - delta.y())
            self._drag_origin = position
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """左键松手：结束拖拽并复位光标。"""
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._apply_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """窗口尺寸变化后重算光标（视口大小变了，可平移性可能变）。"""
        super().resizeEvent(event)
        self._apply_cursor()

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
        self._dragging = False
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("（未选择记录）")
        self.path_label.setText("")
        self.scale_label.setText("—")
        self._apply_cursor()

    def style_hint(self) -> Palette:  # pragma: no cover - 便捷访问
        """返回调色板（供外部统一取色）。"""
        return Palette
