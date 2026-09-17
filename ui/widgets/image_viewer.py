"""可缩放图片查看器（ui.widgets.image_viewer）。

人工复核工作台「左图右证据」中**左侧的产品实拍图**查看控件：

  * 支持**缩放**（滚轮 / 按钮 / 适应窗口 / 1:1）；
  * 支持**旋转**（左旋 / 右旋 90°，v0.3.0 需求 2 —— 现场实拍图常见横竖拍颠倒）；
  * 支持**拖拽平移**（放大后按住鼠标左键拖动查看细节）；
  * 加载失败 / 路径为空时给出**明确占位提示**（🔵 缺图时**不加载任何图片**，
    只显示「缺图」文案 + 提示用户「标记待补图 + 备注」，见 FR：🔵 无图预览）。

**v0.3.0 需求 2 —— 旋转**：``_rotation`` ∈ {0, 90, 180, 270}；渲染统一走
:meth:`ImageViewer._display_pixmap`（= 旋转后的 pixmap），故 ``fit_to_window`` /
``is_pannable`` 的尺寸判断天然跟随旋转结果，**不会出现"旋转后裁切/拖不动"**。
换图（``load``）与清空（``clear`` / ``show_message``）自动复位为 0°。

**v0.3.7 需求 1 / 2 / 3 —— 切换条下沉到图片下方 + 命中标红 + 红色五角星**：

  * **切换条位置（需求 1）**：证据图切换按钮原先挂在**右侧证据卡**里（``WorkbenchCard``），
    而该卡是**整卡纵向滚动**的 —— 判定原因一长，切换按钮就被推到折叠线以下，
    现场"看不到切换按钮"。现改为**常驻在图片正下方**（:attr:`ImageViewer.switcher`），
    与图片同处查看器的固定区域，不受右侧卡片内容高度影响。
  * **左右切换（需求 1）**：新增 ``◀ 上一张`` / ``下一张 ▶`` 两个按钮 ——
    原先只能点具体图号，逐张前后翻必须精确点中按钮，效率低。
  * **命中标红（需求 2）**：**命中申报要素**的图，其切换按钮用**红色**；
    其余用**主题色**（``Palette.ACCENT`` 蓝）。命中信息由上层（工作台）按
    ``CheckResult.token_matches`` 计算后经 :meth:`ImageViewer.set_images` 注入 ——
    本模块**不重算任何判定**，只做展示。
  * **红色五角星（需求 3）**：当前图命中时，在**图片右上角**叠加红色 ``★``
    （:class:`_ImageStage.paintEvent` 绘制，随缩放 / 旋转自动跟随）。

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

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
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
from ui.widgets.flow_layout import FlowLayout

__all__ = ["ImageViewer"]

_MIN_SCALE = 0.05
_MAX_SCALE = 12.0
#: 旋转步长（度）
_ROTATION_STEP = 90
#: 旋转角度固定取值（0/90/180/270）
_ROTATIONS = (0, 90, 180, 270)

#: 「未命中」按钮的主题色（= :data:`ui.styles.palette.Palette.ACCENT`）
_SWITCH_THEME_COLOR: str = Palette.ACCENT
#: 「命中申报要素」按钮与五角星的红色
_SWITCH_HIT_COLOR: str = Palette.DANGER


def _switch_button_qss(*, hit: bool, active: bool) -> str:
    """构造切换条上「图 N」按钮的样式（**命中红 / 未命中主题色**，需求 2）。

    Args:
        hit: 该图是否命中申报要素。
        active: 该图是否为当前正在查看的图。

    Returns:
        内联 QSS 字符串（四态：命中×选中 / 命中×未选中 / 未命中×选中 / 未命中×未选中）。
    """
    color = _SWITCH_HIT_COLOR if hit else _SWITCH_THEME_COLOR
    if active:
        background, foreground, weight = color, "#FFFFFF", "font-weight:bold;"
    else:
        background, foreground, weight = "#FFFFFF", color, ""
    return (
        f"QPushButton{{background:{background};color:{foreground};"
        f"border:1px solid {color};border-radius:6px;padding:4px 10px;{weight}}}"
    )


class _ImageStage(QLabel):
    """图片舞台（``QLabel`` 子类）：在图片**右上角**绘制红色五角星（v0.3.7 需求 3）。

    **为什么直接画在 label 上、而不是叠一个子控件**：本查看器在
    ``setWidgetResizable(False)`` 下把 label 的尺寸**钉在缩放后的图片尺寸**上
    （见 :meth:`ImageViewer._render` 的 ``image_label.resize(scaled.size())``），
    故「label 的右上角」恒等价于「图片的右上角」，且缩放 / 旋转后**自动跟随**，
    不需要任何额外的几何同步代码。

    ⚠️ 只在**真的显示了图片**（``pixmap()`` 非空）时才画 —— 缺图 / 加载失败时
    label 显示的是纯文本占位语，此时画星会误导复核人。
    """

    #: 五角星绘制区边长（px）
    _STAR_SIZE: int = 28
    #: 距右上角内边距（px）
    _STAR_PAD: int = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造图片舞台。"""
        super().__init__(parent)
        self._star: bool = False

    def set_star(self, flag: bool) -> None:
        """设置/清除「命中」五角星（值未变时不重绘）。"""
        value = bool(flag)
        if value != self._star:
            self._star = value
            self.update()

    def star(self) -> bool:
        """返回当前是否显示五角星。"""
        return self._star

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """先画 QLabel 自身内容，再叠加右上角红色五角星。"""
        super().paintEvent(event)
        if not self._star:
            return
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size, pad = self._STAR_SIZE, self._STAR_PAD
        x = max(0, self.width() - size - pad)
        y = pad
        # 半透明白底：深色实拍图上红色五角星同样清晰（不加底会糊成一块）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 215))
        painter.drawEllipse(x - 3, y - 3, size + 6, size + 6)

        font = QFont(self.font())
        font.setPointSize(16)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(_SWITCH_HIT_COLOR))
        painter.drawText(QRect(x, y, size, size), Qt.AlignmentFlag.AlignCenter, "★")
        painter.end()



class ImageViewer(QFrame):
    """可缩放 / 可平移的图片查看器（含图片下方的证据图切换条）。

    Signals:
        load_failed: 加载图片失败（携带路径与原因）。
        image_requested: 用户在切换条上选了另一张图（携带目标路径）。
            ⚠️ 本控件**只发信号、不自己换图** —— 换图需同时同步右侧证据卡，
            由 :class:`ui.widgets.review_workbench.ReviewWorkbench` 统一处理，
            避免"图切了、右边 OCR 没跟着变"的经典不一致。

    Args:
        parent: Qt 父控件。
    """

    load_failed = Signal(str, str)
    image_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造图片查看器。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._pixmap: QPixmap | None = None
        self._scale: float = 1.0
        self._current_path: str = ""
        #: 当前旋转角度（0/90/180/270，v0.3.0 需求 2）
        self._rotation: int = 0
        #: 拖拽平移状态机（按下 → 移动 → 松手）
        self._dragging: bool = False
        self._drag_origin: QPoint = QPoint()
        #: 切换条数据：``[(seq, path, is_hit), ...]``（v0.3.7 需求 1/2）
        self._image_items: list[tuple[int, str, bool]] = []
        #: 图号按钮：``path → 按钮``（供高亮 / 命中标红 / 测试查询）
        self._image_buttons: dict[str, QPushButton] = {}
        #: 切换条上的「当前图」路径（高亮 + 五角星的依据，可能与 `_current_path` 短暂不一致）
        self._selected_path: str = ""
        #: 空的切换条提示标签（无图时显示）
        self._switch_hint: QLabel | None = None
        self._build_ui()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建查看器布局（工具栏 / 图片 / **切换条** / 路径）。"""
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
        # v0.3.0 需求 2：旋转（现场实拍图横竖拍颠倒时摆正）
        self.btn_rotate_left = QPushButton("↺ 左旋", self)
        self.btn_rotate_right = QPushButton("↻ 右旋", self)
        self.btn_rotate_left.setToolTip("逆时针旋转 90°")
        self.btn_rotate_right.setToolTip("顺时针旋转 90°")
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.btn_fit.clicked.connect(self.fit_to_window)
        self.btn_actual.clicked.connect(self.reset_zoom)
        self.btn_rotate_left.clicked.connect(self.rotate_left)
        self.btn_rotate_right.clicked.connect(self.rotate_right)
        for btn in (
            self.btn_zoom_out,
            self.btn_zoom_in,
            self.btn_fit,
            self.btn_actual,
            self.btn_rotate_left,
            self.btn_rotate_right,
        ):
            bar.addWidget(btn)
        self.scale_label = QLabel("100%", self)
        self.scale_label.setMinimumWidth(56)
        self.scale_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.scale_label)
        self.rotation_label = QLabel("0°", self)
        self.rotation_label.setMinimumWidth(40)
        self.rotation_label.setToolTip("当前旋转角度")
        bar.addWidget(self.rotation_label)
        outer.addLayout(bar)

        # 滚动区 + 图片标签
        self.scroll = QScrollArea(self)
        # 点 9.1：关掉 WidgetResizable —— label 保持自身尺寸（= 缩放后图片尺寸），
        # 图片超出视口时滚动条才会出现，从而支持拖动平移。
        self.scroll.setWidgetResizable(False)
        # 图片小于视口时居中显示；大于视口时该对齐被忽略、按需出现滚动条。
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # v0.3.7 需求 3：换用 _ImageStage（在图片右上角叠加命中五角星）
        self.image_label = _ImageStage(self)
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

        # ── v0.3.7 需求 1：证据图切换条（**紧贴图片下方**，常驻不随卡片滚动）──
        outer.addWidget(self._build_switcher())

        self.path_label = QLabel("", self)
        self.path_label.setObjectName("reviewHint")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self.path_label)

    def _build_switcher(self) -> QWidget:
        """构建图片下方的证据图切换条（需求 1 / 2 / 3）。

        **为什么必须放在图片下方（而不是右侧证据卡里）**：右侧证据卡是
        **整卡纵向滚动**的（v0.3.1 设计取向：宁可滚动，不可压扁）—— 判定原因一长，
        原本挂在卡片里的图号按钮就被推到折叠线以下，现场表现为"看不到切换按钮"。
        切到这里后它与图片同处一个固定高度区域（图片区拉伸 1、切换条自然高），
        **永远可见**。

        布局：``[◀ 上一张] [图 1 图 2 …（流式换行）] [下一张 ▶] [★ 命中提示]``。
        """
        holder = QFrame(self)
        holder.setObjectName("imageSwitcher")
        holder.setStyleSheet(
            "QFrame#imageSwitcher{background:#F5F7FA;border:1px solid #DCDFE6;"
            "border-radius:6px;}"
        )
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)

        # 左右切换（需求 1）：逐张前后翻，不必精确点中图号
        self.btn_prev_image = QPushButton("◀ 上一张", holder)
        self.btn_next_image = QPushButton("下一张 ▶", holder)
        self.btn_prev_image.setToolTip("切换到上一张证据图（←）")
        self.btn_next_image.setToolTip("切换到下一张证据图（→）")
        for btn in (self.btn_prev_image, self.btn_next_image):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{background:#FFFFFF;color:{_SWITCH_THEME_COLOR};"
                f"border:1px solid {_SWITCH_THEME_COLOR};border-radius:6px;"
                "padding:4px 10px;}"
            )
        self.btn_prev_image.clicked.connect(lambda: self.step_image(-1))
        self.btn_next_image.clicked.connect(lambda: self.step_image(1))
        row.addWidget(self.btn_prev_image)

        # 图号按钮（流式，图多时自动换行；命中态标红）
        self.thumbs_area = QWidget(holder)
        self.thumbs_layout = FlowLayout(self.thumbs_area, margin=0)
        row.addWidget(self.thumbs_area, 1)
        row.addWidget(self.btn_next_image)

        # 命中提示（需求 3 的文字版，与图片右上角五角星互补）
        self.hit_label = QLabel("", holder)
        self.hit_label.setStyleSheet(
            f"color:{_SWITCH_HIT_COLOR};font-weight:bold;"
        )
        row.addWidget(self.hit_label)
        return holder


    # ─────────────────────── 公开 API ───────────────────────

    def show_message(self, text: str) -> None:
        """显示纯文本占位（无图 / 缺图场景，**不加载图片**）。"""
        self._pixmap = None
        self._current_path = ""
        self._dragging = False
        self._reset_rotation()
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(text)
        self.path_label.setText("")
        self.scale_label.setText("—")
        # 无图 → 不得留星（否则占位文案上顶着一颗"命中"星，误导复核）
        self.image_label.set_star(False)
        self._sync_switcher()
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
        # 换图 → **自动摆正**（需求 2：旋转不跨图记忆，避免上一张的角度误用）
        self._reset_rotation()
        self.path_label.setText(
            f"路径：{path}　（{pix.width()}×{pix.height()}）"
        )
        self._scale = 1.0
        self._render()
        # 首次加载自动适应窗口
        self.fit_to_window()
        # 直接 load（不经 set_current_image）时同步切换条高亮与五角星
        if self._selected_path != path:
            self._selected_path = path
        self._sync_switcher()
        return True

    # ─────────── v0.3.7 需求 1/2/3：证据图切换条 ───────────

    def set_images(self, items: list[tuple[int, str, bool]] | None = None) -> None:
        """重建图片下方的切换条（图号按钮 + 命中标红）。

        Args:
            items: ``[(seq, path, is_hit), ...]`` —— 只应传**存在且可预览**的图；
                ``is_hit`` 由调用方（工作台）按 ``CheckResult.token_matches`` 判定，
                **本控件不重算任何判定**。``None`` / 空序列 → 清空并显示占位提示。
        """
        self._clear_thumb_buttons()
        self._image_items = [
            (int(seq or 0), str(path or ""), bool(hit))
            for seq, path, hit in (items or [])
            if str(path or "").strip()
        ]

        if not self._image_items:
            hint = QLabel("（无可预览图片）", self.thumbs_area)
            hint.setStyleSheet(f"color:{Palette.TEXT_WEAK};")
            self.thumbs_layout.addWidget(hint)
            hint.show()
            self._switch_hint = hint
        else:
            for seq, path, hit in self._image_items:
                btn = QPushButton(f"图{seq}", self.thumbs_area)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(
                    ("★ 本图命中申报要素" if hit else "本图未命中申报要素")
                    + f"\n{path}"
                )
                btn.clicked.connect(lambda _=False, p=path: self._request_image(p))
                self._image_buttons[path] = btn
                self.thumbs_layout.addWidget(btn)
                # ⚠️ 同 WorkbenchCard._reveal：addWidget 的显示是 queued 的，
                #    不显式 show() 则当拍 isHidden() → FlowLayout 高度算成 0
                btn.show()

        if self._selected_path not in self._image_buttons:
            self._selected_path = ""
        self._sync_switcher()

    def image_paths(self) -> list[str]:
        """返回切换条中的图片路径顺序（供测试与外部定位）。"""
        return [path for _seq, path, _hit in self._image_items]

    def hit_image_paths(self) -> set[str]:
        """返回**命中申报要素**的图片路径集合（切换条数据里标记为 hit 的那些）。"""
        return {path for _seq, path, hit in self._image_items if hit}

    def set_current_image(self, path: str) -> None:
        """设置切换条上的「当前图」（只改高亮 / 五角星，**不加载图片**）。"""
        target = str(path or "").strip()
        self._selected_path = target
        self._sync_switcher()

    def current_image_path(self) -> str:
        """返回切换条上的当前图路径（未设置时为空串）。"""
        return self._selected_path

    def step_image(self, delta: int) -> None:
        """按偏移切换图片（``-1`` 上一张 / ``+1`` 下一张，越界夹紧）。"""
        paths = self.image_paths()
        if not paths:
            return
        try:
            index = paths.index(self._selected_path)
        except ValueError:
            index = 0 if delta > 0 else len(paths) - 1
        target = max(0, min(index + int(delta), len(paths) - 1))
        self._request_image(paths[target])

    def _request_image(self, path: str) -> None:
        """请求切换到某张图：先本地高亮，再发信号由工作台统一换图。"""
        if not path:
            return
        self.set_current_image(path)
        self.image_requested.emit(path)

    def _sync_switcher(self) -> None:
        """按「当前图 + 命中集」刷新切换条：按钮四态配色、左右按钮可用性、五角星。

        四态配色（需求 2）：
          * 命中 × 选中 → **红底白字**；命中 × 未选中 → 白底红字红边；
          * 未命中 × 选中 → **主题蓝底白字**；未命中 × 未选中 → 白底主题蓝字蓝边。
        """
        if not hasattr(self, "thumbs_layout"):
            return
        hits = self.hit_image_paths()
        for path, btn in self._image_buttons.items():
            active = path == self._selected_path
            hit = path in hits
            btn.setProperty("evidenceCurrent", bool(active))
            btn.setProperty("evidenceHit", bool(hit))
            btn.setStyleSheet(_switch_button_qss(hit=hit, active=active))

        paths = self.image_paths()
        index = paths.index(self._selected_path) if self._selected_path in paths else -1
        has_prev = index > 0
        has_next = 0 <= index < len(paths) - 1
        if index < 0 and paths:
            # 未选中任何图（如刚换记录）→ 默认后续可前进
            has_prev, has_next = False, True
        self.btn_prev_image.setEnabled(has_prev)
        self.btn_next_image.setEnabled(has_next)

        # 五角星：**仅当该图真的显示在舞台上**时才点亮（占位文案上不画星）
        displayed = (
            self._pixmap is not None
            and bool(self._selected_path)
            and self._selected_path == self._current_path
        )
        star = displayed and self._selected_path in hits
        self.image_label.set_star(star)
        self.hit_label.setText("★ 本图命中申报要素" if star else "")

    def _clear_thumb_buttons(self) -> None:
        """清空切换条上的全部图号按钮 / 占位提示。

        ⚠️ **不能只调 ``deleteLater()``**：它是**异步**的（下一轮事件循环才真正析构），
        而 ``takeAt(0)`` 只是把控件从**布局**里摘掉 —— 控件此时仍是 ``thumbs_area``
        的**子控件**，于是**继续按旧坐标渲染**。连续多次 ``set_images()`` 时会看到
        **上一轮的图号按钮残影**（实测：`图1 图2 图4` 换一轮后变成
        `图1 图2 图4` ＋ 残留的 `图4 图6`）。

        故必须 **``hide()`` + ``setParent(None)`` 同步解除可见性**，再交给
        ``deleteLater()`` 回收（与 ``WorkbenchCard._clear_layout`` 同一处理）。
        """
        if not hasattr(self, "thumbs_layout"):
            return
        while self.thumbs_layout.count():
            item = self.thumbs_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._image_buttons.clear()
        self._switch_hint = None


    # ─────────────────────── 旋转（v0.3.0 需求 2）───────────────────────

    def rotate_left(self) -> None:
        """逆时针旋转 90°（左旋）。"""
        self._rotate_by(-_ROTATION_STEP)

    def rotate_right(self) -> None:
        """顺时针旋转 90°（右旋）。"""
        self._rotate_by(_ROTATION_STEP)

    def current_rotation(self) -> int:
        """返回当前旋转角度（0/90/180/270）。"""
        return self._rotation

    def _rotate_by(self, degrees: int) -> None:
        """按角度增量旋转（无图时忽略；旋转后自动适应窗口避免旋出屏幕）。"""
        if self._pixmap is None:
            return
        self._rotation = (self._rotation + int(degrees)) % 360
        self._render()
        self.fit_to_window()

    def _reset_rotation(self) -> None:
        """复位旋转角度为 0° 并刷新角度标签。"""
        self._rotation = 0
        if hasattr(self, "rotation_label"):
            self.rotation_label.setText("0°")

    def display_pixmap(self) -> QPixmap | None:
        """返回**旋转后**的显示用 pixmap（无图时 ``None``）。

        所有尺寸相关计算（缩放 / 适应窗口 / 可平移性）都基于它，
        保证"旋转 → 尺寸变了 → 滚动条与拖动同步正确"。
        """
        if self._pixmap is None:
            return None
        if self._rotation % 360 == 0:
            return self._pixmap
        return self._pixmap.transformed(
            QTransform().rotate(self._rotation), Qt.TransformationMode.SmoothTransformation
        )

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
        """缩放到适应可视区（基于**旋转后**尺寸）。"""
        pixmap = self.display_pixmap()
        if pixmap is None:
            return
        viewport = self.scroll.viewport().size()
        pw = max(1, pixmap.width())
        ph = max(1, pixmap.height())
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
        """按当前缩放渲染图片（**旋转后**的 pixmap）。"""
        pixmap = self.display_pixmap()
        if pixmap is None:
            return
        pw = max(1, int(pixmap.width() * self._scale))
        ph = max(1, int(pixmap.height() * self._scale))
        scaled = pixmap.scaled(
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
        self.rotation_label.setText(f"{self._rotation}°")
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
        """清空并复位（含旋转角度、切换条与命中五角星）。"""
        self._pixmap = None
        self._current_path = ""
        self._scale = 1.0
        self._dragging = False
        self._reset_rotation()
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("（未选择记录）")
        self.image_label.set_star(False)
        self.path_label.setText("")
        self.scale_label.setText("—")
        # v0.3.7：换记录 → 清掉上一记录的图号按钮（否则残留旧记录的按钮）
        self.set_images([])
        self._apply_cursor()

    def style_hint(self) -> Palette:  # pragma: no cover - 便捷访问
        """返回调色板（供外部统一取色）。"""
        return Palette
