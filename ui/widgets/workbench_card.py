"""复核工作台卡片（ui.widgets.workbench_card）。

「左图右证据」布局中的**右半证据卡**：对**单条记录**展示

  * 记录身份（出货号 / 料号 / 订单 / key）；
  * 当前判定（着色徽标）；
  * **判定链路（四段式）**（v0.2.0 点 9.3）——
    ① 申报要素 → ② 图片识别（+ 证据来源）→ ③ 判定原因（逐字符差异）→ ④ 判定结果；
  * **当前选中图**的 OCR 文本（v0.2.0 点 9.2：切换图片只显示当前图，不再整条拼接）；
  * **KV 明细**（v0.2.0 点 9.4）：主区 `键 | 值 | 置信度` 表格 + 副区散行标签卡
    + 可折叠「查看原始 OCR 全文」；
  * **重判下拉**（``WORKBENCH_VERDICTS`` —— 四类判定，人工可改成任意一类）；
  * **备注输入框**；
  * **「标记待补图」复选框**（🔵 缺图场景：无图可看，只能标记 + 备注）；
  * **「保存重判」按钮**。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑：判定结果
（``verdict`` / ``detected_*`` / ``differences``）**只读取、不重算**。
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.constants import VERDICT_TEXT, WORKBENCH_VERDICTS
from core.models import CheckResult, ImageEvidence, OcrText
from ui.styles.palette import VERDICT_COLORS, Palette
from ui.widgets.flow_layout import FlowLayout

__all__ = ["WorkbenchCard"]

#: 散行标签卡配色（**深色面 + 浅色字**，适配本机深色主题，避免白底黑字）
_LABEL_CARD_QSS = (
    "background:#2B2B2B; color:#E0E0E0; border:1px solid #444444;"
    "border-radius:6px; padding:4px 8px;"
)
#: KV 表格配色（深色面 + 浅色字）
_KV_TABLE_QSS = (
    "QTableWidget{background:#1E1E1E;color:#D4D4D4;gridline-color:#3C3C3C;"
    "border:1px solid #3C3C3C;border-radius:6px;}"
    "QHeaderView::section{background:#2B2B2B;color:#D4D4D4;border:none;padding:4px;}"
    "QTableWidget::item:selected{background:#1565C0;color:#FFFFFF;}"
)
#: 证据图号按钮：当前选中态高亮
_SEQ_BTN_QSS = "border-radius:6px; padding:4px 10px;"
_SEQ_BTN_ACTIVE_QSS = (
    "background:#1565C0; color:#FFFFFF; border:1px solid #1565C0;"
    "border-radius:6px; padding:4px 10px; font-weight:bold;"
)
#: 连续空白（弱分隔）正则，用于把一行拆出「键」半段
_WS_SPLIT_RE = re.compile(r"[ \t\u3000]{2,}")
_KV_SEPARATORS: tuple[str, ...] = (":", "：", "=")


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

        # ── 点 9.3：判定链路（四段式）──
        outer.addWidget(self._build_chain())

        # ── 证据图切换器（图号按钮，当前选中态高亮）──
        outer.addWidget(QLabel("证据图（点击切换）：", self))
        self.evidence_buttons_area = QWidget(self)
        # FlowLayout 以 area 为父 → 构造即安装为该控件布局（图号多时自动换行）
        self.evidence_buttons_row = FlowLayout(self.evidence_buttons_area, margin=0)
        outer.addWidget(self.evidence_buttons_area)

        # ── 点 9.4：当前图 OCR 明细（KV 表 + 散行标签卡 + 折叠原文）──
        outer.addWidget(QLabel("当前图 OCR 明细（键值对齐）：", self))
        self.kv_table = QTableWidget(0, 3, self)
        self.kv_table.setHorizontalHeaderLabels(["键", "值", "置信度"])
        self.kv_table.verticalHeader().setVisible(False)
        self.kv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.kv_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.kv_table.setMaximumHeight(150)
        self.kv_table.setStyleSheet(_KV_TABLE_QSS)
        header = self.kv_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        outer.addWidget(self.kv_table)

        outer.addWidget(QLabel("散行文本（未成键值对）：", self))
        self.residue_area = QWidget(self)
        # FlowLayout 以 area 为父 → 构造即安装为该控件布局（散行多时自动换行）
        self.residue_flow = FlowLayout(self.residue_area, margin=0)
        outer.addWidget(self.residue_area)

        self.raw_toggle = QToolButton(self)
        self.raw_toggle.setText("查看原始 OCR 全文 ▸")
        self.raw_toggle.setCheckable(True)
        self.raw_toggle.setChecked(False)
        self.raw_toggle.toggled.connect(self._on_raw_toggled)
        outer.addWidget(self.raw_toggle)

        self.raw_view = QPlainTextEdit(self)
        self.raw_view.setReadOnly(True)
        self.raw_view.setMaximumHeight(160)
        self.raw_view.setVisible(False)
        outer.addWidget(self.raw_view)

        # ── 点 9.2：当前选中图的 OCR 文本（只显当前图）──
        outer.addWidget(QLabel("当前图 OCR 文本：", self))
        self.evidence_view = QPlainTextEdit(self)
        self.evidence_view.setReadOnly(True)
        self.evidence_view.setMaximumBlockCount(2000)
        self.evidence_view.setMinimumHeight(110)
        outer.addWidget(self.evidence_view)

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
        """构建「判定链路」区块（点 9.3：四段式）。

        Returns:
            组装好的链路段落控件（同时把 4 个段落 :class:`QLabel` 存入
            ``self.chain_labels``，键：``decl`` / ``detected`` / ``reason`` / ``verdict``）。
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

        self.chain_labels: dict[str, QLabel] = {}
        for key in ("decl", "detected", "reason", "verdict"):
            label = QLabel("", frame)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.chain_labels[key] = label
            layout.addWidget(label)
        return frame

    # ─────────────────────── 载入记录 ───────────────────────

    def load_result(self, result: CheckResult) -> None:
        """载入一条记录结果并刷新控件。

        Args:
            result: 校验结果。
        """
        self._result = result
        # 换记录 → 重置当前图（由 _render 兜底选「第一条存在图」）
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
        self.verdict_badge.setText(f"当前判定：{result.verdict.value}")
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
        elif result.verdict.value in ("⚠️ 缺图内标识，人工复核", "🔵 缺图，人工复核"):
            self.hint_label.setText("该条为「待人工复核」类，请核对证据后重判或标记待补图。")
        else:
            self.hint_label.setText("")
        self.setEnabled(True)

    def set_current_image(self, path: str) -> None:
        """切换「当前查看的图片」并只重渲染该图的证据（点 9.2）。

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

    # ─────────────────────── 点 9.2：只渲染当前图 ───────────────────────

    def _render_current_image(self, result: CheckResult) -> None:
        """渲染「当前选中图」的 OCR 文本 + KV 明细（点 9.2 / 9.4）。"""
        evidence = self._current_evidence(result)
        self._render_evidence(result, evidence)
        self._render_kv(evidence)

    def _render_evidence(
        self, result: CheckResult, evidence: ImageEvidence | None = None
    ) -> None:
        """渲染**仅当前选中图**的 OCR 文本（点 9.2）。

        原缺陷：把该记录**全部**图片的 OCR 一次性拼进 ``evidence_view``（11 张图
        即 11 段拼接）。现改为只渲染当前图。
        """
        self.evidence_view.clear()
        if evidence is None:
            evidence = self._current_evidence(result)
        if evidence is None:
            self.evidence_view.setPlainText("（无图片证据）")
            return
        flag = self._evidence_flag(evidence)
        lines = [
            f"— 图 {getattr(evidence, 'seq', 0)}：{getattr(evidence, 'image_path', '')}{flag}"
        ]
        ocr = getattr(evidence, "ocr", None)
        text = getattr(ocr, "text_raw", "") if ocr is not None else ""
        if text:
            lines.append(text)
        else:
            lines.append("（无文本 / 未识别）")
        self.evidence_view.setPlainText("\n".join(lines))

    def _render_kv(self, evidence: ImageEvidence | None) -> None:
        """渲染当前图的 KV 表（主区）+ 散行标签卡（副区）+ 原始全文（折叠区）。"""
        ocr: OcrText | None = getattr(evidence, "ocr", None) if evidence is not None else None

        # ── 主区：KV 表 ──
        rows = self._kv_rows(ocr)
        self.kv_table.setRowCount(0)
        for key, value, confidence in rows:
            row = self.kv_table.rowCount()
            self.kv_table.insertRow(row)
            self.kv_table.setItem(row, 0, QTableWidgetItem(key))
            self.kv_table.setItem(row, 1, QTableWidgetItem(value))
            conf_text = f"{confidence:.0%}" if confidence > 0 else "—"
            self.kv_table.setItem(row, 2, QTableWidgetItem(conf_text))

        # ── 副区：散行标签卡（流式）──
        residue = self._residue_lines(ocr)
        self._fill_flow_labels(self.residue_flow, residue, empty_hint="（无散行文本）")

        # ── 折叠区：原始 OCR 全文 ──
        self.raw_view.setPlainText(getattr(ocr, "text_raw", "") if ocr is not None else "")
        self.raw_toggle.setChecked(False)
        self.raw_view.setVisible(False)

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

    # ─────────────────────── 点 9.3：判定链路 ───────────────────────

    def _render_chain(self, result: CheckResult) -> None:
        """渲染四段式判定链路（数据全部来自既有模型，不改判定引擎）。"""
        record = getattr(result, "record", None)
        decl_brand = getattr(record, "decl_brand", "") if record is not None else ""
        decl_model = getattr(record, "decl_model", "") if record is not None else ""
        detected_brand = getattr(result, "detected_brand", "") or ""
        detected_model = getattr(result, "detected_model", "") or ""

        # ① 申报要素
        self.chain_labels["decl"].setText(
            "<b>① 申报要素</b>　"
            f"品牌 = {self._or_none(decl_brand)}　型号 = {self._or_none(decl_model)}"
        )

        # ② 图片识别（跨图投票结果 + 证据来源）
        brand_hint = self._brand_source_hint(result, detected_brand)
        self.chain_labels["detected"].setText(
            "<b>② 图片识别</b>　"
            f"品牌 = {self._or_none(detected_brand)}　型号 = {self._or_none(detected_model)}"
            f"<br/><span style='color:{Palette.TEXT_WEAK}'>{brand_hint}</span>"
        )

        # ③ 判定原因（优先差异明细的逐字符差异）
        self.chain_labels["reason"].setText(
            "<b>③ 判定原因</b>　" + self._reason_text(result)
        )

        # ④ 判定结果（判定 + 一句话理由）
        reason = (getattr(result, "reason", "") or "").strip()
        verdict_text = VERDICT_TEXT.get(result.verdict, result.verdict.value)
        tail = f"　—　{html.escape(reason)}" if reason else ""
        self.chain_labels["verdict"].setText(
            f"<b>④ 判定结果</b>　{html.escape(verdict_text)}{tail}"
        )

    def _reason_text(self, result: CheckResult) -> str:
        """由差异明细（``differences``）拼装 ③ 段文案；无差异时回退一句话理由。"""
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

    def _brand_source_hint(self, result: CheckResult, brand: str) -> str:
        """推断「② 图片识别」品牌的证据来源图号（**best-effort，UI 侧）**。

        现有模型**没有**「投票来源图号」字段（跨图投票只回吐最终值），故此处按
        「哪几张图的 OCR 原文含该品牌文本」做保守推断；推断不出则显式标注
        「未记录」，绝不臆造。**不触碰判定引擎**。

        Args:
            result: 校验结果。
            brand: 图片识别品牌（空则不推断）。

        Returns:
            形如 ``（品牌来自 图9、图12 等 3 张图）`` 或 ``（证据来源未记录）``。
        """
        target = (brand or "").strip().upper()
        if not target:
            return "（图片未识别到品牌）"
        seqs: list[int] = []
        for ev in self._evidences(result):
            ocr = getattr(ev, "ocr", None)
            text = (getattr(ocr, "text_raw", "") if ocr is not None else "") or ""
            if target in text.upper():
                seqs.append(int(getattr(ev, "seq", 0) or 0))
        if not seqs:
            return "（证据来源未记录）"
        shown = "、".join(f"图{seq}" for seq in seqs[:5])
        extra = f" 等 {len(seqs)} 张图" if len(seqs) > 1 else ""
        return f"（品牌来自 {shown}{extra}）"

    # ─────────────────────── 点 9.4：KV / 散行 解析 ───────────────────────

    def _kv_rows(self, ocr: OcrText | None) -> list[tuple[str, str, float]]:
        """把 ``OcrText.kv`` 解析为 ``[(键, 值, 置信度)]``（置信度取对应行分数）。"""
        if ocr is None:
            return []
        kv = dict(getattr(ocr, "kv", {}) or {})
        if not kv:
            return []
        lines = ocr.lines()
        scores = list(getattr(ocr, "line_scores", []) or [])
        default_conf = float(getattr(ocr, "confidence", 0.0) or 0.0)
        rows: list[tuple[str, str, float]] = []
        for key, value in kv.items():
            confidence = default_conf
            key_upper = (key or "").strip().upper()
            for idx, line in enumerate(lines):
                if self._line_key(line) == key_upper:
                    if idx < len(scores):
                        confidence = float(scores[idx])
                    break
            rows.append((str(key), str(value), confidence))
        return rows

    def _residue_lines(self, ocr: OcrText | None) -> list[str]:
        """返回当前图中**未成键值对**的散行文本。"""
        if ocr is None:
            return []
        lines = ocr.lines()
        kv = dict(getattr(ocr, "kv", {}) or {})
        if not kv:
            return lines
        keys = {(k or "").strip().upper() for k in kv}
        return [line for line in lines if self._line_key(line) not in keys]

    @staticmethod
    def _line_key(line: str) -> str:
        """取一行的「键」半段（按分隔符 / 连续空白切分），大写去空白。"""
        text = (line or "").strip()
        for sep in _KV_SEPARATORS:
            idx = text.find(sep)
            if idx >= 0:
                return text[:idx].strip().upper()
        match = _WS_SPLIT_RE.search(text)
        if match is not None:
            return text[: match.start()].strip().upper()
        return text.upper()

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
        """空值显示为「（无）」，非空原样返回（HTML 转义）。"""
        text = (value or "").strip()
        return html.escape(text) if text else "（无）"

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

    def _on_raw_toggled(self, checked: bool) -> None:
        """折叠/展开「原始 OCR 全文」。"""
        self.raw_view.setVisible(bool(checked))
        self.raw_toggle.setText("收起原始 OCR 全文 ▾" if checked else "查看原始 OCR 全文 ▸")

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
        for label in getattr(self, "chain_labels", {}).values():
            label.setText("")
        self._clear_layout(self.evidence_buttons_row)
        self.evidence_view.clear()
        self.kv_table.setRowCount(0)
        self._clear_layout(self.residue_flow)
        self.raw_view.clear()
        self.raw_view.setVisible(False)
        self.raw_toggle.setChecked(False)
        self.note_edit.clear()
        self.mark_missing_check.setChecked(False)
        self.hint_label.clear()
        self.setEnabled(False)

    def current_key(self) -> str:
        """返回当前记录 key（无记录时为空串）。"""
        return self._result.key if self._result is not None else ""
