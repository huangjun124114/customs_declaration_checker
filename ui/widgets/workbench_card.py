"""复核工作台卡片（ui.widgets.workbench_card）。

「左图右证据」布局中的**右半证据卡**：对**单条记录**展示

  * 记录身份（出货号 / 料号 / 订单 / key）；
  * 当前判定（着色徽标）；
  * **证据 OCR 文本**（按图片序号列出，可复制）；
  * **重判下拉**（``WORKBENCH_VERDICTS`` —— 四类判定，人工可改成任意一类）；
  * **备注输入框**；
  * **「标记待补图」复选框**（🔵 缺图场景：无图可看，只能标记 + 备注）；
  * **「保存重判」按钮**。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.constants import VERDICT_TEXT, WORKBENCH_VERDICTS
from core.models import CheckResult
from ui.styles.palette import VERDICT_COLORS, Palette

__all__ = ["WorkbenchCard"]


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

        # 证据 OCR 文本
        outer.addWidget(QLabel("证据 OCR 文本：", self))
        self.evidence_view = QPlainTextEdit(self)
        self.evidence_view.setReadOnly(True)
        self.evidence_view.setMaximumBlockCount(2000)
        self.evidence_view.setMinimumHeight(140)
        outer.addWidget(self.evidence_view)

        # 证据跳转（图号按钮）
        self.evidence_buttons_row = QHBoxLayout()
        self.evidence_buttons_row.setSpacing(6)
        outer.addLayout(self.evidence_buttons_row)

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

    # ─────────────────────── 载入记录 ───────────────────────

    def load_result(self, result: CheckResult) -> None:
        """载入一条记录结果并刷新控件。

        Args:
            result: 校验结果。
        """
        self._result = result
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

        self._render_evidence(result)
        self._render_evidence_buttons(result)

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

    def _render_evidence(self, result: CheckResult) -> None:
        """渲染证据 OCR 文本。"""
        self.evidence_view.clear()
        lines: list[str] = []
        evidences = list(getattr(result, "evidence_images", []) or [])
        if not evidences and getattr(result, "record", None) is not None:
            evidences = list(getattr(result.record, "evidences", []) or [])
        if not evidences:
            lines.append("（无图片证据）")
        for ev in evidences:
            flag = ""
            if getattr(ev, "unreachable", False):
                flag = "［共享盘不可达］"
            elif getattr(ev, "not_found", False):
                flag = "［目录不存在 / 缺图］"
            elif not getattr(ev, "exists", False):
                flag = "［文件缺失］"
            lines.append(f"— 图 {getattr(ev, 'seq', 0)}：{getattr(ev, 'image_path', '')}{flag}")
            ocr = getattr(ev, "ocr", None)
            text = getattr(ocr, "text_raw", "") if ocr is not None else ""
            if text:
                lines.append(f"    OCR：{text}")
            else:
                lines.append("    OCR：（无文本 / 未识别）")
        self.evidence_view.setPlainText("\n".join(lines))

    def _render_evidence_buttons(self, result: CheckResult) -> None:
        """为每张存在的证据图生成跳转按钮。"""
        # 清空旧按钮
        while self.evidence_buttons_row.count():
            item = self.evidence_buttons_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        evidences = list(getattr(result, "evidence_images", []) or [])
        if not evidences and getattr(result, "record", None) is not None:
            evidences = list(getattr(result.record, "evidences", []) or [])
        shown = False
        for ev in evidences:
            if not getattr(ev, "exists", False):
                continue
            shown = True
            path = str(getattr(ev, "image_path", ""))
            btn = QPushButton(f"图{getattr(ev, 'seq', 0)}", self)
            btn.clicked.connect(lambda _=False, p=path: self.evidence_selected.emit(p))
            self.evidence_buttons_row.addWidget(btn)
        if not shown:
            self.evidence_buttons_row.addWidget(QLabel("（无可预览图片）", self))
        self.evidence_buttons_row.addStretch(1)

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
        self.identity_label.setText("（未选择记录）")
        self.verdict_badge.setText("")
        self.verdict_badge.setStyleSheet("")
        self.evidence_view.clear()
        while self.evidence_buttons_row.count():
            item = self.evidence_buttons_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.note_edit.clear()
        self.mark_missing_check.setChecked(False)
        self.hint_label.clear()
        self.setEnabled(False)

    def current_key(self) -> str:
        """返回当前记录 key（无记录时为空串）。"""
        return self._result.key if self._result is not None else ""
