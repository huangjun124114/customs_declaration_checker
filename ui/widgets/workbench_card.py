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
  * **重判下拉**（``WORKBENCH_VERDICTS`` —— 四类判定，人工可改成任意一类）；
  * **备注输入框**；
  * **「标记待补图」复选框**（🔵 缺图场景：无图可看，只能标记 + 备注）；
  * **「保存重判」按钮**。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑：判定结果
（``verdict`` / ``detected_*`` / ``differences`` / ``token_matches``）**只读取、不重算**。
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.constants import FIELD_BRAND, FIELD_MODEL, VERDICT_TEXT, WORKBENCH_VERDICTS
from core.models import CheckResult, ImageEvidence, OcrText, Verdict
from core.noise_guard import is_none_token
from core.token_matcher import MATCH_FUZZY, TokenMatch
from ui.styles.palette import VERDICT_COLORS, Palette
from ui.widgets.flow_layout import FlowLayout
from ui.widgets.ocr_text_dialog import OcrTextDialog

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
#: 证据图号按钮：普通态 / 当前选中态高亮
_SEQ_BTN_QSS = "border-radius:6px; padding:4px 10px;"
_SEQ_BTN_ACTIVE_QSS = (
    "background:#1565C0; color:#FFFFFF; border:1px solid #1565C0;"
    "border-radius:6px; padding:4px 10px; font-weight:bold;"
)

#: 判定链路三列表头（需求 5 原文口径）
_CHAIN_HEADERS: tuple[str, ...] = ("要素", "申报值", "判定值")
#: 表体两行的字段名（唯一来源 :mod:`core.constants`，防字面量漂移）
_CHAIN_FIELDS: tuple[str, ...] = (FIELD_BRAND, FIELD_MODEL)
#: 判定链路表行高（容 2 行换行文案，避免长判定值被裁切）
_CHAIN_ROW_HEIGHT: int = 40
#: 判定链路表固定高度（= 表头 28 + 2 行 × 行高 + 边框余量）
#: ⚠️ 必须固定：``QTableWidget`` 默认 sizeHint 高 192px，会在表体下方留一大片空白
_CHAIN_TABLE_HEIGHT: int = 28 + 2 * _CHAIN_ROW_HEIGHT + 4


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
        #: 图号按钮：image_path → 按钮（供高亮 / 测试查询）
        self._seq_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self.clear()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建卡片布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("证据 / 重判", self)
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

        # ── 证据图切换器（图号按钮，当前选中态高亮）──
        outer.addWidget(QLabel("证据图（点击切换）：", self))
        self.evidence_buttons_area = QWidget(self)
        # FlowLayout 以 area 为父 → 构造即安装为该控件布局（图号多时自动换行）
        self.evidence_buttons_row = FlowLayout(self.evidence_buttons_area, margin=0)
        outer.addWidget(self.evidence_buttons_area)

        # ── 需求 7：当前图 OCR 散行文本（唯一展示形态，无 KV 表格）──
        line_header = QHBoxLayout()
        line_header.addWidget(QLabel("当前图 OCR 文本（散行）：", self))
        line_header.addStretch(1)
        self.btn_view_ocr = QPushButton("查看原始 OCR 文本", self)
        self.btn_view_ocr.setToolTip("以弹窗展示本图 OCR 全文（弹窗不会遮挡图片区）")
        self.btn_view_ocr.clicked.connect(self._on_view_ocr)
        line_header.addWidget(self.btn_view_ocr)
        outer.addLayout(line_header)

        self.line_area = QWidget(self)
        # FlowLayout 以 area 为父 → 构造即安装为该控件布局（散行多时自动换行）
        self.line_flow = FlowLayout(self.line_area, margin=0)
        outer.addWidget(self.line_area)

        # ── 需求 8：原始 OCR 弹窗（单例，非模态；不遮挡图片区）──
        self.ocr_dialog = OcrTextDialog(self)
        self.ocr_dialog.previous_requested.connect(lambda: self._step_image(-1))
        self.ocr_dialog.next_requested.connect(lambda: self._step_image(1))

        # 重判行
        judge_row = QHBoxLayout()
        judge_row.addWidget(QLabel("重判为：", self))
        self.verdict_combo = QComboBox(self)
        for verdict in WORKBENCH_VERDICTS:
            display = VERDICT_TEXT.get(verdict, verdict.value)
            self.verdict_combo.addItem(display, verdict.value)
        judge_row.addWidget(self.verdict_combo, 1)
        outer.addLayout(judge_row)

        # 备注
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("备注：", self))
        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("填写复核说明 / 补图要求（可选）")
        note_row.addWidget(self.note_edit, 1)
        outer.addLayout(note_row)

        # 标记待补图 + 保存
        action_row = QHBoxLayout()
        self.mark_missing_check = QCheckBox("标记待补图", self)
        self.mark_missing_check.setToolTip("🔵 缺图记录：无图可看，仅能标记待补图 + 备注")
        self.btn_save = QPushButton("保存重判", self)
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.clicked.connect(self._on_save)
        action_row.addWidget(self.mark_missing_check)
        action_row.addStretch(1)
        action_row.addWidget(self.btn_save)
        outer.addLayout(action_row)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("reviewHint")
        self.hint_label.setWordWrap(True)
        outer.addWidget(self.hint_label)

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
        header = self.chain_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, field in enumerate(_CHAIN_FIELDS):
            self.chain_table.setItem(row, 0, QTableWidgetItem(field))
            self.chain_table.setRowHeight(row, _CHAIN_ROW_HEIGHT)
        self.chain_table.setFixedHeight(_CHAIN_TABLE_HEIGHT)
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
        self._render_evidence_buttons(result)
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

    def set_current_image(self, path: str) -> None:
        """切换「当前查看的图片」并只重渲染该图的证据。

        由 :class:`ui.widgets.review_workbench.ReviewWorkbench` 在切换左侧查看器
        图片时**同步**调用。

        Args:
            path: 目标图片路径；为空时回退到「第一条存在图」。
        """
        if self._result is None:
            self._current_image_path = str(path or "")
            return
        target = str(path or "").strip()
        if target != self._current_image_path:
            self._current_image_path = target
        self._render_evidence_buttons(self._result)
        self._render_current_image(self._result)

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

    def _render_evidence_buttons(self, result: CheckResult) -> None:
        """为每张存在的证据图生成跳转按钮；给**当前选中态加高亮**。"""
        self._clear_layout(self.evidence_buttons_row)
        self._seq_buttons.clear()

        evidences = self._evidences(result)
        if self._current_image_path not in {
            str(getattr(ev, "image_path", "")) for ev in evidences
        }:
            # 当前路径不在本记录中 → 回退到「第一条存在图」
            self._current_image_path = self._default_image_path(result)

        shown = False
        for ev in evidences:
            if not getattr(ev, "exists", False):
                continue
            shown = True
            path = str(getattr(ev, "image_path", ""))
            seq = getattr(ev, "seq", 0)
            btn = QPushButton(f"图{seq}", self.evidence_buttons_area)
            btn.clicked.connect(lambda _=False, p=path: self._on_seq_button(p))
            self._seq_buttons[path] = btn
            self._style_seq_button(btn, active=(path == self._current_image_path))
            self.evidence_buttons_row.addWidget(btn)
        if not shown:
            hint = QLabel("（无可预览图片）", self.evidence_buttons_area)
            hint.setStyleSheet("color:#909399;")
            self.evidence_buttons_row.addWidget(hint)

    def _on_seq_button(self, path: str) -> None:
        """图号按钮点击：切换当前图并通知外部（左侧查看器）。"""
        self.set_current_image(path)
        self.evidence_selected.emit(path)

    def _style_seq_button(self, button: QPushButton, *, active: bool) -> None:
        """按当前选中态刷新按钮样式与可测试属性。"""
        button.setProperty("evidenceCurrent", bool(active))
        button.setStyleSheet(_SEQ_BTN_ACTIVE_QSS if active else _SEQ_BTN_QSS)

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
             ``NONE`` → ❌ 图内为「detected」/ ⚠️ 图内未出现{field}文字 / 申报为无
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

    # ─────────────────────── 需求 8：原始 OCR 弹窗 ───────────────────────

    def _on_view_ocr(self) -> None:
        """点击「查看原始 OCR 文本」→ 刷新并显示**非模态弹窗**（单例）。

        弹窗定位在**主窗口右侧外**（或屏幕右缘、图片区右边界之外），
        **不遮挡图片区**（需求 8 关键约束）。
        """
        if self._result is None:
            return
        self._refresh_ocr_dialog()
        self.ocr_dialog.show_beside(main_window=self.window(), avoid=self._image_area())

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
        """清空布局中的全部控件（带 deleteLater）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _fill_flow_labels(self, layout, texts: list[str], *, empty_hint: str) -> None:
        """把 ``texts`` 逐条渲染为流式小标签卡（深色面 + 浅色字）。"""
        self._clear_layout(layout)
        parent = layout.parentWidget()
        if not texts:
            hint = QLabel(empty_hint, parent)
            hint.setStyleSheet("color:#909399;")
            layout.addWidget(hint)
            return
        for text in texts:
            card = QLabel(text, parent)
            card.setWordWrap(True)
            card.setStyleSheet(_LABEL_CARD_QSS)
            card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(card)

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
        self._seq_buttons.clear()
        self.identity_label.setText("（未选择记录）")
        self.verdict_badge.setText("")
        self.verdict_badge.setStyleSheet("")
        self._clear_chain()
        self._clear_layout(self.evidence_buttons_row)
        self._clear_layout(self.line_flow)
        if self.ocr_dialog.isVisible():
            self.ocr_dialog.hide()
        self.note_edit.clear()
        self.mark_missing_check.setChecked(False)
        self.hint_label.clear()
        self.setEnabled(False)

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
