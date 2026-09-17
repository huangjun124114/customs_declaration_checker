"""复核工作台卡片（ui.widgets.workbench_card）。

「左图右证据」布局中的**右半证据卡**：对**单条记录**展示

  * 记录身份（出货号 / 料号 / 订单 / key）；
  * 当前判定（着色徽标）；
  * **判定链路（三列，v0.3.0 需求 5）** —— ``要素 | 申报值 | 判定值`` 两行
    （品牌 / 型号）。「判定值」列的数据源是引擎回吐的
    :class:`core.token_matcher.TokenMatch`（**真实命中 token + 命中图号**），
    不再由 UI 侧"文本包含"近似推断（v0.3 遗留 #3 已闭环）；
  * **当前选中图**的 OCR **散行文本**（v0.3.0 需求 7：**废弃 KV 表格**，
    全部改为散行标签卡；``OcrText.kv`` 仍提取、仍落详细 JSON，只是不进 UI）；
  * **「查看原始 OCR 文本」按钮**（v0.3.0 需求 8）→ 打开**非模态弹窗**
    :class:`ui.widgets.ocr_text_dialog.OcrTextDialog`，**不遮挡图片区**；
  * **「查看申报要素」按钮**（v0.3.3）→ 打开同款的
    :class:`ui.widgets.ocr_text_dialog.DeclarationTextDialog`，展示本条记录
    ``record.raw_element_text``（**记录级**原文，**原样**，不解析不清洗）。
    与 OCR 弹窗共用右侧定位 → **互斥**（开一个即关另一个，避免完全叠窗）；
  * **重判下拉**（``WORKBENCH_VERDICTS`` —— 四类判定，人工可改成任意一类）；
  * **备注输入框**；
  * **「标记待补图」复选框**（🔵 缺图场景：无图可看，只能标记 + 备注）；
  * **「保存重判」按钮**。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑：判定结果
（``verdict`` / ``detected_*`` / ``differences`` / ``token_matches``）**只读取、不重算**。
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.constants import FIELD_BRAND, FIELD_MODEL, VERDICT_TEXT, WORKBENCH_VERDICTS
from core.models import CheckResult, DeclarationRecord, ImageEvidence, OcrText, Verdict
from core.noise_guard import is_none_token
from core.token_matcher import MATCH_FUZZY, TokenMatch
from ui.styles.palette import VERDICT_COLORS, Palette
from ui.widgets.flow_layout import FlowLayout
from ui.widgets.ocr_text_dialog import DeclarationTextDialog, OcrTextDialog

__all__ = ["WorkbenchCard"]

#: 散行标签卡配色（**浅色面 + 深色字** —— 与 ``ui/styles/app.qss`` 的浅色主题一致；
#: v0.3.0 修正：v0.2.0 曾用「深色面 + 浅色字」，但本应用全局是**浅色主题**，
#: 深色卡片落在白卡上像渲染异常，现统一为浅色）
_LABEL_CARD_QSS = (
    "background:#F5F7FA; color:#303133; border:1px solid #DCDFE6;"
    "border-radius:6px; padding:4px 8px;"
)
#: 判定链路三列表配色（浅色面 + 深色字，同卡片主题）
_CHAIN_TABLE_QSS = (
    "QTableWidget{background:#FFFFFF;color:#303133;gridline-color:#E4E7ED;"
    "border:1px solid #DCDFE6;border-radius:6px;}"
    "QHeaderView::section{background:#F5F7FA;color:#303133;border:none;"
    "padding:4px;font-weight:bold;}"
)

#: 判定链路三列表头（需求 5 原文口径）
_CHAIN_HEADERS: tuple[str, ...] = ("要素", "申报值", "判定值")
#: 表体两行的字段名（唯一来源 :mod:`core.constants`，防字面量漂移）
_CHAIN_FIELDS: tuple[str, ...] = (FIELD_BRAND, FIELD_MODEL)
#: 判定链路表行高（容 2 行换行文案，避免长判定值被裁切）
_CHAIN_ROW_HEIGHT: int = 40
#: 前两列的**固定列宽**（要素 / 申报值）；第三列「判定值」用 Stretch 吃掉余量。
#: ⚠️ 必须用 Fixed 而非 ResizeToContents：``ResizeToContents`` + ``Stretch`` 混用时，
#: 列宽随内容与 splitter 拖动反复重算，表头与单元格的几何可能不同步（视觉"错位"）。
_CHAIN_COL_ELEMENT: int = 72
_CHAIN_COL_DECLARED: int = 150
#: 判定链路表**初始**高度（表头 28 + 2 行 × 行高 + 边框余量）。
#: ⚠️ 运行期由 :meth:`WorkbenchCard._fit_chain_height` 按**表头实际高**校正 ——
#: ``QTableWidget`` 默认 ``sizeHint`` 高 192px 会在表体下留大片空白；而把高度
#: 写死成常数，又会在表头随字体/DPI 变高时裁掉末行并冒出纵向滚动条（实测"错位覆盖"根因）。
_CHAIN_TABLE_FALLBACK_HEIGHT: int = 28 + 2 * _CHAIN_ROW_HEIGHT + 4


class WorkbenchCard(QFrame):
    """单条记录的复核卡（右半证据区）。

    Signals:
        verdict_changed: 用户保存重判（``key, new_verdict, note, mark_missing``）。
        evidence_selected: 选中某张证据图（携带其路径，供左侧查看器加载）。

    Args:
        parent: Qt 父控件。
    """

    verdict_changed = Signal(str, str, str, bool)
    evidence_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造复核卡。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._result: CheckResult | None = None
        #: 当前选中（右侧正在展示 OCR 的）证据图路径
        self._current_image_path: str = ""
        #: `_fit_content_height` 重入闸（视口 Resize ↔ 内容 resize 会互相触发）
        self._fitting_content: bool = False
        self._build_ui()
        self.clear()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建卡片布局。

        **为什么整卡套一层 ``QScrollArea``（v0.3.1 现场反馈修正）**：
        卡片内容高度 = 标题 + 身份 + 判定徽标 + 判定链路（2 行表 + 判定原因 + 判定结果）
        + 证据图按钮 + **散行 OCR（行数不定）** + 重判行 + 备注行 + 操作行。
        当内容总高 > 窗格高时，``QVBoxLayout`` 会把子控件压到**最小尺寸以下** ——
        实测判定链路框被压成 190px（内容实需 228px），于是「判定原因」叠到表格的
        「型号」行上，正是现场看到的"错位覆盖"。

        ⚠️ **只加滚动区还不够**：``widgetResizable(True)`` 默认把内容控件拉到
        **视口高**（实测 759），首选高（``sizeHint`` 1274）压根不参与分配。
        真正生效的是 :meth:`_fit_content_height` —— 用 ``heightForWidth`` 取
        **按当前宽度换行后真正需要的高度**（实测 886，远小于过度估计的 1274）
        并写入 ``minimumHeight``，内容随宽度自适应改按 886 分配，
        链路框因此拿到 228px（≥ 最小 194），**重叠消失**，超出部分交给滚动条。

        ⚠️ **底部操作区必须留在滚动区外**（``QFrame#cardFooter``）：
        「重判为 / 备注 / 标记待补图 / 保存重判」是**每次复核都要用**的动作；
        若一起放进滚动区，内容一超高按钮就被推到折叠线以下，得先滚动才够得着 ——
        那只是把"压扁"换成了"藏起来"。故 shell = 滚动区(拉伸) + 底部操作区(常驻)。
        """
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("cardScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 横向不滚动：内部 FlowLayout 已按可用宽度自动换行
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # 拉伸因子 1：窗口变高时把多出的高度给滚动区，底部操作区保持自然高
        shell.addWidget(self.scroll, 1)

        # 视口宽度一变（滚动条出现/消失、splitter 拖动）→ 散行换行数变 → 重算内容高
        self.scroll.viewport().installEventFilter(self)

        content = QWidget(self.scroll)
        content.setObjectName("cardScrollContent")
        self.scroll.setWidget(content)

        # ⚠️ 以下所有控件都挂到 ``content`` 下（由布局自动 reparent）
        outer = QVBoxLayout(content)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("证据 / 重判", content)
        title.setObjectName("zoneTitle")
        outer.addWidget(title)

        # 身份行
        self.identity_label = QLabel("", self)
        self.identity_label.setWordWrap(True)
        self.identity_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self.identity_label)

        # 判定徽标
        self.verdict_badge = QLabel("", self)
        self.verdict_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.verdict_badge.setMinimumHeight(28)
        outer.addWidget(self.verdict_badge)

        # ── 需求 5：判定链路（三列：要素 / 申报值 / 判定值）──
        outer.addWidget(self._build_chain())

        # ⚠️ v0.3.7 需求 1：**证据图切换按钮已迁出本卡片**，改挂在图片查看器正下方
        #    （:attr:`ui.widgets.image_viewer.ImageViewer.switcher`）。
        #    原因：本卡是**整卡纵向滚动**的（v0.3.1 设计取向"宁可滚动，不可压扁"），
        #    判定原因一长，证据图按钮就被推到折叠线以下 —— 现场正是"看不到切换按钮"。
        #    迁到图片下方后与图片同处固定高度区，**永远可见**，且新增了左右翻页。

        # ── 需求 7：当前图 OCR 散行文本（唯一展示形态，无 KV 表格）──
        line_header = QHBoxLayout()
        line_header.addWidget(QLabel("当前图 OCR 文本（散行）：", self))
        line_header.addStretch(1)
        self.btn_view_ocr = QPushButton("查看原始 OCR 文本", self)
        self.btn_view_ocr.setToolTip("以弹窗展示本图 OCR 全文（弹窗不会遮挡图片区）")
        self.btn_view_ocr.clicked.connect(self._on_view_ocr)
        line_header.addWidget(self.btn_view_ocr)

        # ── v0.3.3：申报要素原文入口（记录级，紧邻 OCR 按钮）──
        self.btn_view_decl = QPushButton("查看申报要素", self)
        self.btn_view_decl.setToolTip(
            "以弹窗展示本条记录在申报要素表中的整段原文（未经解析清洗；不会遮挡图片区）"
        )
        self.btn_view_decl.clicked.connect(self._on_view_decl)
        line_header.addWidget(self.btn_view_decl)
        outer.addLayout(line_header)

        self.line_area = QWidget(self)
        # FlowLayout 以 area 为父 → 构造即安装为该控件布局（散行多时自动换行）
        self.line_flow = FlowLayout(self.line_area, margin=0)
        outer.addWidget(self.line_area)

        # ── 需求 8：原始 OCR 弹窗（单例，非模态；不遮挡图片区）──
        self.ocr_dialog = OcrTextDialog(self)
        self.ocr_dialog.previous_requested.connect(lambda: self._step_image(-1))
        self.ocr_dialog.next_requested.connect(lambda: self._step_image(1))

        # ── v0.3.3：申报要素原文弹窗（记录级；与 OCR 弹窗**互斥**，见 _on_view_decl）──
        self.decl_dialog = DeclarationTextDialog(self)

        # ── 底部操作区（**常驻，不参与滚动**）──
        self.footer = QFrame(self)
        self.footer.setObjectName("cardFooter")
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(12, 6, 12, 10)
        footer.setSpacing(8)
        shell.addWidget(self.footer, 0)

        # 重判行
        judge_row = QHBoxLayout()
        judge_row.addWidget(QLabel("重判为：", self.footer))
        self.verdict_combo = QComboBox(self.footer)
        for verdict in WORKBENCH_VERDICTS:
            display = VERDICT_TEXT.get(verdict, verdict.value)
            self.verdict_combo.addItem(display, verdict.value)
        judge_row.addWidget(self.verdict_combo, 1)
        footer.addLayout(judge_row)

        # 备注
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("备注：", self.footer))
        self.note_edit = QLineEdit(self.footer)
        self.note_edit.setPlaceholderText("填写复核说明 / 补图要求（可选）")
        note_row.addWidget(self.note_edit, 1)
        footer.addLayout(note_row)

        # 标记待补图 + 保存
        action_row = QHBoxLayout()
        self.mark_missing_check = QCheckBox("标记待补图", self.footer)
        self.mark_missing_check.setToolTip("🔵 缺图记录：无图可看，仅能标记待补图 + 备注")
        self.btn_save = QPushButton("保存重判", self.footer)
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.clicked.connect(self._on_save)
        action_row.addWidget(self.mark_missing_check)
        action_row.addStretch(1)
        action_row.addWidget(self.btn_save)
        footer.addLayout(action_row)

        self.hint_label = QLabel("", self.footer)
        self.hint_label.setObjectName("reviewHint")
        self.hint_label.setWordWrap(True)
        footer.addWidget(self.hint_label)

    def _build_chain(self) -> QWidget:
        """构建「判定链路」区块（需求 5：三列 ``要素 | 申报值 | 判定值``）。

        Returns:
            组装好的链路段落控件（表体 2 行 × 3 列：品牌 / 型号）。
        """
        frame = QFrame(self)
        frame.setObjectName("chainFrame")
        frame.setStyleSheet(
            "QFrame#chainFrame{background:#F5F7FA;border:1px solid #DCDFE6;"
            "border-radius:6px;}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        caption = QLabel("判定链路", frame)
        caption.setStyleSheet("font-weight:bold;color:#303133;")
        layout.addWidget(caption)

        self.chain_table = QTableWidget(len(_CHAIN_FIELDS), len(_CHAIN_HEADERS), frame)
        self.chain_table.setHorizontalHeaderLabels(list(_CHAIN_HEADERS))
        self.chain_table.verticalHeader().setVisible(False)
        self.chain_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chain_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.chain_table.setShowGrid(True)
        self.chain_table.setWordWrap(True)
        self.chain_table.setStyleSheet(_CHAIN_TABLE_QSS)
        # ⚠️ 关闭双滚动条：表高按内容自适应。滚动条一旦出现会同时
        #    ① 挤掉右侧列宽（表头与单元格几何不同步）② 把「型号」行推出视口，
        #    与下方「判定原因」挤在一起 → 就是实测反馈的"错位覆盖"。
        self.chain_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.chain_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        header = self.chain_table.horizontalHeader()
        # ⚠️ QHeaderView 默认**居中**对齐，而单元格默认左对齐 → 表头文字与列内容
        #    看起来"错位"。统一为左对齐（与单元格一致）。
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setMinimumSectionSize(56)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.chain_table.setColumnWidth(0, _CHAIN_COL_ELEMENT)
        self.chain_table.setColumnWidth(1, _CHAIN_COL_DECLARED)
        for row, field in enumerate(_CHAIN_FIELDS):
            self.chain_table.setItem(row, 0, QTableWidgetItem(field))
            self.chain_table.setRowHeight(row, _CHAIN_ROW_HEIGHT)
        self.chain_table.setFixedHeight(_CHAIN_TABLE_FALLBACK_HEIGHT)
        layout.addWidget(self.chain_table)

        # ③ 判定原因（差异明细 / 一句话理由）—— 保留可追溯红线
        self.chain_reason = QLabel("", frame)
        self.chain_reason.setWordWrap(True)
        self.chain_reason.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.chain_reason)

        # ④ 判定结果
        self.chain_verdict = QLabel("", frame)
        self.chain_verdict.setWordWrap(True)
        self.chain_verdict.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.chain_verdict)
        return frame

    def _fit_chain_height(self) -> None:
        """按「**表头实际高** + 各行高 + 边框」校正判定链路表高度。

        **为什么不能写死常数**：表头高度随字体 / DPI / QSS 的 ``padding`` 变化
        （实测在某些机器上是 30px、某些是 34px）。写死 112px 时表头一变高，
        纵向滚动条就会出现 —— 滚动条既挤掉右侧列宽（表头与单元格几何不同步 → 错位），
        又把「型号」行推出视口（与下方「判定原因」视觉重叠）。此处按实测值算，
        既**不留空白**（``QTableWidget`` 默认 ``sizeHint`` 高 192px）也不裁行。
        """
        table = self.chain_table
        height = table.horizontalHeader().height() + 2 * table.frameWidth() + 2
        for row in range(table.rowCount()):
            height += table.rowHeight(row)
        table.setFixedHeight(height)

    def _fit_content_height(self) -> None:
        """把滚动内容的**最小高度**校正为「按当前宽度换行后真正需要的高度」。

        **为什么必须显式做**：``QScrollArea(widgetResizable=True)`` 只会把内容控件
        拉到 ``max(视口高, 内容最小高)``；内容布局的最小高（实测 600）远小于它
        **真正需要**的高（886，其中散行 OCR 换行后就要 279）。于是内容被钉在
        视口高 759 上，``QVBoxLayout`` 只能在「最小 ~ 首选」之间**压缩**子控件 ——
        判定链路框被压到 190（< 最小 194），「判定原因」便叠到表格的「型号」行上。

        这里取 ``heightForWidth`` 而非 ``sizeHint``：后者按「标签不被换行」估算，
        实测高达 1274（过度估计 → 白滚动）；前者按实际宽度算换行，实测 886。

        ⚠️ 宽度依赖 → 必须在**宽度变化**（splitter 拖动 / 窗口缩放）与
        **内容变化**（换记录 / 切图 → 散行数变化）后都重算。
        """
        if self._fitting_content:
            return
        content = self.scroll.widget()
        layout = content.layout() if content is not None else None
        if content is None or layout is None:
            return
        self._fitting_content = True
        try:
            width = int(self.scroll.viewport().width())
            if width <= 0:
                width = int(content.width())
            need = content.heightForWidth(width) if content.hasHeightForWidth() else -1
            if need <= 0:
                # 极端兜底：无 hfw 时退回首选高（宁可多滚一点，也不裁内容）
                need = int(layout.sizeHint().height())
            if need <= 0:
                return
            if need != content.minimumHeight():
                content.setMinimumHeight(need)
            content.resize(max(width, content.width()), need)
        finally:
            self._fitting_content = False

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # noqa: N802 - Qt 规定
        """滚动视口 Resize → 重算内容高（防「滚动条出现后列宽变窄」导致裁内容）。"""
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_content_height()
        return super().eventFilter(obj, event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt 规定的驼峰名
        """显示时校正一次判定链路表高度（此刻表头高度才最终确定）。"""
        super().showEvent(event)
        self._relayout()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt 规定的驼峰名
        """宽度变化 → 散行标签换行数变化 → 重新校正内容高度。"""
        super().resizeEvent(event)
        self._fit_content_height()

    def _relayout(self) -> None:
        """内容/尺寸变化后的统一校正入口：先表高、再滚动内容高。

        ⚠️ 这里**不做延迟补算**：新增子控件由 :meth:`_reveal` 同步 ``show()``
        之后，当拍测得的高度即为稳定值（实测 886）。若改用
        ``QTimer.singleShot`` 补算，反而可能在「子控件瞬时不可见」的一拍里
        算出偏小值并**写回更小的 minimumHeight**，把压缩缺陷重新引回来。
        宽度变化由滚动视口的 ``Resize`` 事件过滤兜底（见 :meth:`eventFilter`）。
        """
        self._fit_chain_height()
        self._fit_content_height()

    # ─────────────────────── 载入记录 ───────────────────────

    def load_result(self, result: CheckResult) -> None:
        """载入一条记录结果并刷新控件。

        Args:
            result: 校验结果。
        """
        self._result = result
        # 换记录 → 重置当前图（由 _render_* 兜底选「第一条存在图」）
        self._current_image_path = ""
        record = getattr(result, "record", None)
        color = VERDICT_COLORS.get(result.verdict.value, Palette.TEXT)

        ticket = getattr(record, "ticket_no", "") if record is not None else ""
        part = getattr(record, "part_no", "") if record is not None else ""
        order = getattr(record, "order_no", "") if record is not None else ""
        self.identity_label.setText(
            f"<b>出货号</b>：{ticket or '—'}　"
            f"<b>料号</b>：{part or '—'}　"
            f"<b>订单</b>：{order or '—'}<br/>"
            f"<span style='color:{Palette.TEXT_WEAK}'>key：{result.key}</span>"
        )
        self.verdict_badge.setText(
            f"当前判定：{VERDICT_TEXT.get(result.verdict, result.verdict.value)}"
        )
        self.verdict_badge.setStyleSheet(
            f"color: #FFFFFF; background: {color}; border-radius: 6px; font-weight: bold;"
        )

        self._render_chain(result)
        self._render_current_image(result)

        # 预置重判下拉为当前判定
        idx = self.verdict_combo.findData(result.verdict.value)
        if idx >= 0:
            self.verdict_combo.setCurrentIndex(idx)

        self.note_edit.setText(getattr(result, "reviewer_note", "") or "")
        self.mark_missing_check.setChecked(bool(getattr(result, "mark_missing", False)))

        reviewed = bool(getattr(result, "manually_reviewed", False))
        if reviewed:
            self.hint_label.setText(
                "⚠ 此条已人工复核过——再次保存将覆盖本轮结果（幂等：以最终一次为准）"
            )
        elif result.verdict in (Verdict.NO_MARK, Verdict.NO_IMAGE):
            self.hint_label.setText("该条为「待人工复核」类，请核对证据后重判或标记待补图。")
        else:
            self.hint_label.setText("")
        self.setEnabled(True)
        # 内容整体换过一轮（链路 / 图号 / 散行）→ 重新校正高度
        self._relayout()

    def set_current_image(self, path: str) -> None:
        """切换「当前查看的图片」并只重渲染该图的证据。

        由 :class:`ui.widgets.review_workbench.ReviewWorkbench` 在切换图片时
        **同步**调用（切换条点选 / 左右翻页 / 弹窗翻页 / 换记录均走这里）。

        Args:
            path: 目标图片路径；为空时回退到「第一条存在图」。
        """
        if self._result is None:
            self._current_image_path = str(path or "")
            return
        target = str(path or "").strip()
        if target != self._current_image_path:
            self._current_image_path = target
        self._render_current_image(self._result)
        # 散行数随图片变化 → 内容高度需重算
        self._fit_content_height()

    def current_image_path(self) -> str:
        """返回当前选中（正在展示 OCR）的图片路径。"""
        return self._current_image_path

    # ─────────────────────── 当前图渲染 ───────────────────────

    def _render_current_image(self, result: CheckResult) -> None:
        """渲染「当前选中图」的**散行** OCR 文本（需求 7）。"""
        evidence = self._current_evidence(result)
        self._render_lines(evidence)
        # 弹窗可见时跟随刷新（不自动弹起，避免"切图就跳窗"）
        if self.ocr_dialog.isVisible():
            self._refresh_ocr_dialog()
        # v0.3.3：申报要素弹窗是**记录级**，换记录后若仍开着必须同步换原文
        if self.decl_dialog.isVisible():
            self._refresh_decl_dialog()

    def _render_lines(self, evidence: ImageEvidence | None) -> None:
        """把当前图 OCR **全部行**渲染为散行标签卡（不再剔除已成键值对的行）。

        Args:
            evidence: 当前证据图；``None``/无 OCR 时显示空态提示。
        """
        ocr: OcrText | None = (
            getattr(evidence, "ocr", None) if evidence is not None else None
        )
        lines = list(ocr.lines()) if ocr is not None else []
        self._fill_flow_labels(self.line_flow, lines, empty_hint="（本图无识别文本）")

    def _on_seq_button(self, path: str) -> None:
        """切到某张证据图并通知外部（左侧查看器 / 工作台的**唯一**接线口）。

        ⚠️ v0.3.7 起卡片上已无图号按钮，本方法仍被
        :meth:`_step_image`（OCR 弹窗的「上一张 / 下一张」）复用。
        """
        self.set_current_image(path)
        self.evidence_selected.emit(path)

    # ─────────────────────── 需求 5：判定链路三列 ───────────────────────

    def _render_chain(self, result: CheckResult) -> None:
        """渲染三列判定链路（数据源：引擎回吐的 ``TokenMatch``，UI 只读）。

        Args:
            result: 校验结果。
        """
        record = getattr(result, "record", None)
        for row, field in enumerate(_CHAIN_FIELDS):
            if field == FIELD_BRAND:
                declared = getattr(record, "decl_brand", "") if record is not None else ""
                detected = getattr(result, "detected_brand", "") or ""
            else:
                declared = getattr(record, "decl_model", "") if record is not None else ""
                detected = getattr(result, "detected_model", "") or ""
            match = self._match_for(result, field)
            self.chain_table.setItem(row, 1, self._cell(self._or_none(declared)))
            text, color, tooltip = self._judge_cell(field, detected, match)
            self.chain_table.setItem(row, 2, self._cell(text, color=color, tooltip=tooltip))

        self.chain_reason.setText("<b>判定原因</b>　" + self._reason_text(result))
        reason = (getattr(result, "reason", "") or "").strip()
        verdict_text = VERDICT_TEXT.get(result.verdict, result.verdict.value)
        tail = f"　—　{html.escape(reason)}" if reason else ""
        self.chain_verdict.setText(
            f"<b>判定结果</b>　{html.escape(verdict_text)}{tail}"
        )
        # 文案变化可能改变换行行数 → 同步校正表高（防裁行 / 防空白）
        self._fit_chain_height()

    @staticmethod
    def _match_for(result: CheckResult, field: str) -> TokenMatch | None:
        """取某字段的 :class:`TokenMatch`（缺失返回 ``None`` → 走旧链路回退）。"""
        getter = getattr(result, "token_match_for", None)
        if callable(getter):
            return getter(field)
        for match in getattr(result, "token_matches", []) or []:
            if getattr(match, "field", "") == field:
                return match
        return None

    def _judge_cell(
        self,
        field: str,
        detected: str,
        match: TokenMatch | None,
    ) -> tuple[str, str, str]:
        """生成「判定值」列文案（含颜色与 tooltip）。

        取值优先级（方案 §6）：

          1. ``TokenMatch``（**引擎回吐的真实取证**）：
             ``EXACT`` → ✅ 命中完整分词「token」（图 n）；``FUZZY`` → ✅ + 误读纠正说明；
             ``NONE`` → ✅ 图内显式标注「无品牌/无型号」（v0.3.2 口径）/
             ❌ 图内为「detected」/ ⚠️ 图内未出现{field}文字 / 申报为无
          2. 无 ``TokenMatch``（旧结果集 / 未走新链路）→ 回退 ``detected_*`` 并标注「旧链路」。

        Returns:
            ``(文案, 前景色, tooltip)``。
        """
        if match is None:
            text = (detected or "").strip() or "（无）"
            return (
                f"{text}　（旧链路）",
                Palette.TEXT_WEAK,
                "本条结果无 TokenMatch 字段（旧链路产出），仅能展示图片侧识别值。",
            )

        token = (match.token or "").strip()
        images = self._format_images(match)
        note = (match.note or "").strip()
        tooltip = "\n".join(
            part
            for part in (
                f"命中方式：{match.mode}",
                f"命中 token：{token}" if token else "",
                f"命中图号：{images}" if images else "",
                f"命中原文：{match.sample_line}" if match.sample_line else "",
                note,
            )
            if part
        )

        if match.hit:
            color = VERDICT_COLORS.get(Verdict.PASS.value, Palette.TEXT)
            if match.mode == MATCH_FUZZY:
                wrong = (match.corrected_from or "").strip()
                detail = f"（疑似误读「{wrong}」，已纠正）" if wrong else ""
                return f"✅ 命中「{token}」{detail}{images}", color, tooltip
            return f"✅ 完整分词「{token}」{images}", color, tooltip

        # ── NONE：按方案 §3.1 两级分流语义呈现（文案从简，细节在 tooltip / 判定原因）──
        # 【口径 v0.3.2】图片侧「显式无标记」＋ 申报侧为无 → 双方均为无，判合格
        marker = (getattr(match, "none_marker", "") or "").strip()
        if marker:
            return (
                f"✅ 图内显式标注「{marker}」{images}",
                VERDICT_COLORS.get(Verdict.PASS.value, Palette.TEXT),
                tooltip,
            )
        if is_none_token(match.declared) or not (match.declared or "").strip():
            return "— 申报为无，未参与匹配", Palette.TEXT_WEAK, tooltip
        if "命中作废" in note:
            return "⚠️ 命中作废：图内该词为字段名", Palette.WARN, tooltip
        if (detected or "").strip():
            return (
                f"❌ 图内为「{detected.strip()}」",
                VERDICT_COLORS.get(Verdict.FAIL.value, Palette.TEXT),
                tooltip,
            )
        return f"⚠️ 图内无{field}文字", Palette.WARN, tooltip

    @staticmethod
    def _format_images(match: TokenMatch) -> str:
        """把 ``TokenMatch.images`` 格式化为 ``（图 1、2）``（无则空串）。"""
        images = [int(i) for i in (match.images or []) if int(i or 0) > 0]
        if not images:
            return ""
        shown = "、".join(str(i) for i in images[:5])
        extra = f" 等 {len(images)} 张" if len(images) > 5 else ""
        return f"（图 {shown}{extra}）"

    @staticmethod
    def _cell(text: str, *, color: str = "", tooltip: str = "") -> QTableWidgetItem:
        """构造只读单元格（可选前景色 / tooltip）。"""
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def _reason_text(self, result: CheckResult) -> str:
        """由差异明细（``differences``）拼装判定原因；无差异时回退一句话理由。"""
        differences = list(getattr(result, "differences", []) or [])
        if differences:
            parts: list[str] = []
            for diff in differences:
                field = getattr(diff, "field", "") or ""
                declared = getattr(diff, "declared_value", "") or ""
                detected = getattr(diff, "detected_value", "") or ""
                head = (
                    f"{html.escape(field)}不一致："
                    f"申报「{html.escape(declared)}」≠ 图片「{html.escape(detected)}」"
                )
                char_diffs = list(getattr(diff, "char_diffs", []) or [])
                if char_diffs:
                    head += "；差异点：" + "".join(html.escape(str(c)) for c in char_diffs)
                note = getattr(diff, "note", "") or ""
                if note:
                    head += f"（{html.escape(note)}）"
                parts.append(head)
            return "<br/>".join(parts)
        reason = (getattr(result, "reason", "") or "").strip()
        return html.escape(reason) if reason else "（无差异说明）"

    # ─────────────────────── 需求 8：原文弹窗（OCR / 申报要素）───────────────────────

    def _on_view_ocr(self) -> None:
        """点击「查看原始 OCR 文本」→ 刷新并显示**非模态弹窗**（单例）。

        弹窗定位在**主窗口右侧外**（或屏幕右缘、图片区右边界之外），
        **不遮挡图片区**（需求 8 关键约束）。

        ⚠️ 与「申报要素」弹窗共用右侧定位 → 先关掉对方，避免两窗完全重叠
        （见 :meth:`_on_view_decl`）。
        """
        if self._result is None:
            return
        self.decl_dialog.hide()
        self._refresh_ocr_dialog()
        self.ocr_dialog.show_beside(main_window=self.window(), avoid=self._image_area())

    def _on_view_decl(self) -> None:
        """点击「查看申报要素」→ 以弹窗展示本条记录的**申报要素整段原文**（v0.3.3）。

        弹窗与「原始 OCR 文本」同款：**非模态、右侧、垂直居中、不遮挡图片区**。

        ⚠️ **只读原样**：正文取 ``record.raw_element_text``，**不做** strip / 清洗 /
        截断 / 反解析 —— 项目的「原始输入只进不改」铁律；判定契约也要求
        ``raw_element_text`` 保留原始形态（``docs/04`` 裁决二）。解析出的品牌/型号
        仅作为**附加提示**（:meth:`_refresh_decl_dialog`），不替换原文。
        """
        if self._result is None:
            return
        self.ocr_dialog.hide()
        self._refresh_decl_dialog()
        self.decl_dialog.show_beside(main_window=self.window(), avoid=self._image_area())

    def _refresh_decl_dialog(self) -> None:
        """按当前记录刷新申报要素弹窗（内容与当前图无关）。"""
        record = self._record()
        raw = str(getattr(record, "raw_element_text", "") or "")
        self.decl_dialog.set_content(
            self._decl_dialog_title(record),
            raw if raw else "（本条记录无申报要素原文）",
        )
        self.decl_dialog.set_hint(self._decl_parse_hint(record))

    @staticmethod
    def _decl_dialog_title(record: DeclarationRecord | None) -> str:
        """构造申报要素弹窗标题（**纯函数**，便于单测）。

        Args:
            record: 源申报记录；``None`` 时返回占位标题。

        Returns:
            形如 ``第 3 条 · 料号 N100204 · 订单号 1652AM00 · 票号 SA26090215``
            （缺字段则跳过；全缺则 ``（未知记录）``）。
        """
        if record is None:
            return "（未知记录）"
        parts: list[str] = []
        seq = int(getattr(record, "seq_no", 0) or 0)
        if seq:
            parts.append(f"第 {seq} 条")
        part_no = str(getattr(record, "part_no", "") or "")
        if part_no:
            parts.append(f"料号 {part_no}")
        order_no = str(getattr(record, "order_no", "") or "")
        if order_no:
            parts.append(f"订单号 {order_no}")
        ticket_no = str(getattr(record, "ticket_no", "") or "")
        if ticket_no:
            parts.append(f"票号 {ticket_no}")
        return " · ".join(parts) if parts else "（未知记录）"

    @staticmethod
    def _decl_parse_hint(record: DeclarationRecord | None) -> str:
        """构造申报要素弹窗的**附加提示**：解析出的品牌 / 型号（**纯函数**）。

        用途：让复核人一眼看到"工具从这段原文里取到了什么"，而正文仍是原样原文
        —— 二者并列展示，避免"看到原文却不知道解析结果"的来回切换。

        Args:
            record: 源申报记录。

        Returns:
            提示语；无解析结果时给出明确说明（而非空白）。
        """
        if record is None:
            return ""
        brand = str(getattr(record, "decl_brand", "") or "")
        model = str(getattr(record, "decl_model", "") or "")
        if not brand and not model:
            return "提示：未从该原文解析出品牌 / 型号（可能要素缺失），请人工看图判断。"
        return (
            f"解析结果（仅供参考，正文未改动）：{FIELD_BRAND}={brand or '—'}　"
            f"{FIELD_MODEL}={model or '—'}"
        )

    def _record(self) -> DeclarationRecord | None:
        """返回当前结果的源申报记录（``None`` 表示无记录）。"""
        return getattr(self._result, "record", None) if self._result is not None else None

    def _refresh_ocr_dialog(self) -> None:
        """按当前图刷新弹窗内容（含「上一张 / 下一张」可用性）。"""
        evidence = self._current_evidence(self._result) if self._result else None
        seq = int(getattr(evidence, "seq", 0) or 0)
        path = str(getattr(evidence, "image_path", "") or "")
        ocr: OcrText | None = (
            getattr(evidence, "ocr", None) if evidence is not None else None
        )
        text = "\n".join(ocr.lines()) if ocr is not None else ""
        flag = self._evidence_flag(evidence) if evidence is not None else ""
        title = (
            f"当前图 {seq} — {path}{flag}" if evidence is not None else "（无图片证据）"
        )
        index = self._existing_image_index()
        total = len(self._existing_image_paths())
        self.ocr_dialog.set_content(
            title,
            text or "（本图无识别文本）",
            has_prev=index > 0,
            has_next=0 <= index < total - 1,
        )

    def _image_area(self) -> QWidget | None:
        """返回同工作台中的图片查看器（供弹窗避让）；无则 ``None``。

        ⚠️ 本卡片被 ``QSplitter.addWidget()`` **重新挂到 splitter 下**，
        故 ``parent()`` 是 splitter 而非工作台 —— 必须**向上遍历父链**找
        ``image_viewer``（限深，避免死循环）。
        """
        node = self.parentWidget()
        depth = 0
        while node is not None and depth < 8:
            viewer = getattr(node, "image_viewer", None)
            if isinstance(viewer, QWidget):
                return viewer
            node = node.parentWidget()
            depth += 1
        return None

    def _existing_image_paths(self) -> list[str]:
        """当前记录中**存在**的证据图路径（与图号按钮同序）。"""
        if self._result is None:
            return []
        return [
            str(getattr(ev, "image_path", ""))
            for ev in self._evidences(self._result)
            if getattr(ev, "exists", False)
        ]

    def _existing_image_index(self) -> int:
        """当前图在「存在图」列表中的下标（找不到返回 -1）。"""
        paths = self._existing_image_paths()
        try:
            return paths.index(self._current_image_path)
        except ValueError:
            return -1

    def _step_image(self, delta: int) -> None:
        """弹窗「上一张 / 下一张」：切图并同步左侧查看器。"""
        paths = self._existing_image_paths()
        if not paths:
            return
        index = self._existing_image_index()
        target = index + delta
        if index < 0:
            target = 0 if delta > 0 else len(paths) - 1
        target = max(0, min(target, len(paths) - 1))
        self._on_seq_button(paths[target])

    # ─────────────────────── 内部工具 ───────────────────────

    @staticmethod
    def _evidences(result: CheckResult) -> list[ImageEvidence]:
        """返回该记录的证据图列表（``evidence_images`` 优先，回退 ``record.evidences``）。"""
        evidences = list(getattr(result, "evidence_images", []) or [])
        if not evidences and getattr(result, "record", None) is not None:
            evidences = list(getattr(result.record, "evidences", []) or [])
        return evidences

    @staticmethod
    def _evidence_flag(evidence: ImageEvidence) -> str:
        """返回证据状态标记（不可达 / 目录缺失 / 文件缺失）。"""
        if getattr(evidence, "unreachable", False):
            return "［共享盘不可达］"
        if getattr(evidence, "not_found", False):
            return "［目录不存在 / 缺图］"
        if not getattr(evidence, "exists", False):
            return "［文件缺失］"
        return ""

    def _default_image_path(self, result: CheckResult) -> str:
        """默认当前图 = **第一条存在且可达**的图；无则退第一条；再无以空串。"""
        evidences = self._evidences(result)
        for ev in evidences:
            if getattr(ev, "exists", False) and not getattr(ev, "unreachable", False):
                return str(getattr(ev, "image_path", ""))
        for ev in evidences:
            if getattr(ev, "exists", False):
                return str(getattr(ev, "image_path", ""))
        return str(getattr(evidences[0], "image_path", "")) if evidences else ""

    def _current_evidence(self, result: CheckResult) -> ImageEvidence | None:
        """返回当前选中路径对应的证据对象；未指定时回退默认图。"""
        evidences = self._evidences(result)
        if not evidences:
            return None
        if not self._current_image_path:
            self._current_image_path = self._default_image_path(result)
        for ev in evidences:
            if str(getattr(ev, "image_path", "")) == self._current_image_path:
                return ev
        return evidences[0]

    @staticmethod
    def _or_none(value: str) -> str:
        """空值显示为「（无）」，非空原样返回。"""
        text = (value or "").strip()
        return text if text else "（无）"

    def _clear_layout(self, layout) -> None:
        """清空布局中的全部控件（**同步**解除可见性后再 ``deleteLater``）。

        ⚠️ **不能只调 ``deleteLater()``**（v0.3.7 修）：它是**异步**的，
        而 ``takeAt(0)`` 只把控件从**布局**摘掉 —— 控件仍是父级的**子控件**，
        会**继续按旧坐标渲染**到下轮事件循环。切记录时表现为
        **上一条的散行标签残影**（本项目 §E 已有同源教训：「不进布局≠不可见，
        要真不可见必须显式 ``hide()``」）。
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _fill_flow_labels(self, layout, texts: list[str], *, empty_hint: str) -> None:
        """把 ``texts`` 逐条渲染为流式小标签卡（浅色面 + 深色字）。"""
        self._clear_layout(layout)
        parent = layout.parentWidget()
        if not texts:
            hint = QLabel(empty_hint, parent)
            hint.setStyleSheet("color:#909399;")
            layout.addWidget(hint)
            self._reveal(hint)
            return
        for text in texts:
            card = QLabel(text, parent)
            card.setWordWrap(True)
            card.setStyleSheet(_LABEL_CARD_QSS)
            card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(card)
            self._reveal(card)

    @staticmethod
    def _reveal(widget: QWidget) -> None:
        """立刻让新加入布局的子控件「非隐藏」，使几何查询当拍即准。

        ⚠️ **v0.3.1 关键坑**：``QLayout.addWidget()`` 内部是用
        ``QMetaObject::invokeMethod(w, "_q_showIfNotHidden", QueuedConnection)``
        把新子控件**排队**显示的。在事件循环下一拍之前，这些标签仍然是
        ``isHidden() == True`` → ``QWidgetItem::isEmpty()`` 成立 →
        ``FlowLayout`` 的 ``sizeHint`` / ``heightForWidth`` 全部返回 **0**
        → 上层算出的"内容需要多高"严重偏小（实测 0 / 577 / 718，而非 886），
        子控件遂被压缩，判定链路框只剩 190px → 「判定原因」叠到表格「型号」行。

        显式 ``show()`` 会**同步**清掉 hidden 标记（父链可见时即刻可见），
        后续的撑高计算随即准确。
        """
        widget.show()

    # ─────────────────────── 保存 ───────────────────────

    def _on_save(self) -> None:
        """保存重判。"""
        if self._result is None:
            return
        key = self._result.key
        new_verdict = str(self.verdict_combo.currentData() or self._result.verdict.value)
        note = self.note_edit.text().strip()
        mark_missing = self.mark_missing_check.isChecked()
        self.verdict_changed.emit(key, new_verdict, note, mark_missing)

    # ─────────────────────── 状态 ───────────────────────

    def clear(self) -> None:
        """清空为「未选择」态。"""
        self._result = None
        self._current_image_path = ""
        self.identity_label.setText("（未选择记录）")
        self.verdict_badge.setText("")
        self.verdict_badge.setStyleSheet("")
        self._clear_chain()
        self._clear_layout(self.line_flow)
        if self.ocr_dialog.isVisible():
            self.ocr_dialog.hide()
        # v0.3.3：申报要素弹窗同为**记录级**视图，记录清空后不得滞留旧原文
        if self.decl_dialog.isVisible():
            self.decl_dialog.hide()
        self.note_edit.clear()
        self.mark_missing_check.setChecked(False)
        self.hint_label.clear()
        self.setEnabled(False)
        self._relayout()

    def _clear_chain(self) -> None:
        """清空判定链路三列表体（保留表头）。"""
        for row in range(self.chain_table.rowCount()):
            self.chain_table.setItem(row, 0, QTableWidgetItem(_CHAIN_FIELDS[row]))
            self.chain_table.setItem(row, 1, QTableWidgetItem(""))
            self.chain_table.setItem(row, 2, QTableWidgetItem(""))
        self.chain_reason.setText("")
        self.chain_verdict.setText("")

    def current_key(self) -> str:
        """返回当前记录 key（无记录时为空串）。"""
        return self._result.key if self._result is not None else ""
