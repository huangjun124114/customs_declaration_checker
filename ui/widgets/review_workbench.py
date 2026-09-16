"""人工复核工作台（ui.widgets.review_workbench）。

把「**记录表格**」+「**左图右证据**」+「**重判/备注**」+「**复核后重导出**」组装为一个
独立可切换的工作台面板（架构设计 12.C）：

  * **上**：**状态标签**（全部 / 成功 / 待复核 / 缺图 / 失败，带数量与灯色）
    + **模糊搜索**（出货单号 / 订单号 / 物料编号，大小写不敏感子串包含）；
  * **左**：记录**表格**（订单号 / 物料编号 / 核验结果；选中行整行高亮）；
  * **中**：:class:`ui.widgets.image_viewer.ImageViewer`（🔵 缺图时**不加载图**，只显提示）；
  * **右**：:class:`ui.widgets.workbench_card.WorkbenchCard`（三列判定链路 + 散行 OCR + 重判 + 备注）；
  * **底部**： 「保存本轮留痕并重导出」（``复核留痕/review_round_N.json`` + 覆盖 ``*_复核后.json``）。

**v0.3.0 需求 3 / 4**：列表 → 表格、状态标签带数量、三字段模糊搜索、
选中行高亮并**同步**中间图片与右侧证据卡。

过滤管线：``全部结果 → 状态标签 → 模糊搜索``，任一步为空则表格留空并清空中/右区。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑（重判逻辑委托
:class:`app.review_store.ReviewStore`）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.session import AppSession
from core.constants import REVIEW_VERDICTS, VERDICT_LABELS
from core.models import CheckResult, Verdict
from ui.styles.palette import VERDICT_COLORS, Palette
from ui.widgets.image_viewer import ImageViewer
from ui.widgets.workbench_card import WorkbenchCard

__all__ = ["ReviewWorkbench"]

#: 「全部」标签的取值（与 Verdict.value 区分）
_STATUS_ALL = "all"

#: 状态标签顺序（v0.3.0 需求 3：全部 / 成功 / 待复核 / 缺图 / 失败）
_STATUS_ORDER: tuple[str, ...] = (
    _STATUS_ALL,
    Verdict.PASS.value,
    Verdict.NO_MARK.value,
    Verdict.NO_IMAGE.value,
    Verdict.FAIL.value,
)

#: 状态标签文案（短文案，**不加 emoji** —— 圆点已由颜色承载语义）
_STATUS_TEXT: dict[str, str] = {
    _STATUS_ALL: "全部",
    Verdict.PASS.value: "成功",
    Verdict.NO_MARK.value: "待复核",
    Verdict.NO_IMAGE.value: "缺图",
    Verdict.FAIL.value: "失败",
}

#: 「全部」标签的中性色（其余取 :data:`ui.styles.palette.VERDICT_COLORS`）
_STATUS_ALL_COLOR = "#546E7A"

#: 表格配色（**浅色底 + 深色字**，与 ``ui/styles/app.qss`` 的浅色主题一致；
#: 选中行整行高亮，且**不覆盖**单元格自身前景色 —— 非选中行保留判定色）
_TABLE_QSS = (
    "QTableWidget{background:#FFFFFF;color:#303133;gridline-color:#E4E7ED;"
    "border:1px solid #DCDFE6;border-radius:6px;}"
    "QHeaderView::section{background:#F5F7FA;color:#303133;border:none;"
    "padding:5px;font-weight:bold;}"
    "QTableWidget::item{padding:3px 6px;}"
    "QTableWidget::item:selected{background:#D6E4FF;color:#1A1A1A;font-weight:bold;}"
)


class ReviewWorkbench(QFrame):
    """人工复核工作台（独立面板，可整体显示 / 隐藏）。

    Signals:
        verdict_applied: 已应用一次重判（``key, new_verdict, note, mark_missing``）。
        export_requested: 请求「保存留痕并重导出」。

    Args:
        session: 会话结果集（工作台从中取记录）。
        parent: Qt 父控件。
    """

    verdict_applied = Signal(str, str, str, bool)
    export_requested = Signal()

    def __init__(self, session: AppSession | None = None, parent: QWidget | None = None) -> None:
        """构造复核工作台。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._session = session if session is not None else AppSession()
        #: 当前状态标签；默认「全部」——标签带数量，待复核/缺图不会被静默隐藏，
        #: 且默认即可用搜索直达任意单号（头部仍常驻「待复核：N 条」摘要）
        self._status: str = _STATUS_ALL
        #: 当前表格中显示（过滤后）的 key 顺序（供 ``select_key`` / 测试查询）
        self._keys_in_order: list[str] = []
        #: 状态标签按钮：状态取值 → 按钮（供测试查询）
        self.status_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self.reload()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建工作台布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # ── 头部：标题 + 搜索 + 待复核计数 ──
        header = QHBoxLayout()
        title = QLabel("人工复核工作台", self)
        title.setObjectName("zoneTitle")
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(QLabel("搜索：", self))
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("出货单号 / 订单号 / 物料编号（模糊匹配）")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(240)
        self.search_edit.textChanged.connect(self._on_search_changed)
        header.addWidget(self.search_edit, 1)
        self.pending_label = QLabel("待复核：0 条", self)
        self.pending_label.setStyleSheet(f"color: {Palette.WARN}; font-weight: bold;")
        header.addWidget(self.pending_label)
        outer.addLayout(header)

        # ── 状态标签行（需求 3：带数量 + 灯色）──
        tags = QHBoxLayout()
        tags.setSpacing(6)
        self._status_group = QButtonGroup(self)
        self._status_group.setExclusive(True)
        for status in _STATUS_ORDER:
            btn = QPushButton(self._status_text(status, 0), self)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, s=status: self._on_status_clicked(s))
            self._status_group.addButton(btn)
            self.status_buttons[status] = btn
            tags.addWidget(btn)
        tags.addStretch(1)
        outer.addLayout(tags)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # ── 左：记录表格（需求 4：订单号 / 物料编号 / 核验结果）──
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addWidget(QLabel("记录：", self))
        self.record_table = QTableWidget(0, 3, self)
        self.record_table.setHorizontalHeaderLabels(["订单号", "物料编号", "核验结果"])
        self.record_table.verticalHeader().setVisible(False)
        self.record_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.record_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.record_table.setAlternatingRowColors(False)
        self.record_table.setShowGrid(True)
        # 三列并排：料号最长 17 字 + 核验结果 5 字 → 太窄会把结果列挤成「…」（实测踩坑）
        self.record_table.setMinimumWidth(400)
        self.record_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.record_table.setStyleSheet(_TABLE_QSS)
        header_view = self.record_table.horizontalHeader()
        header_view.setMinimumSectionSize(72)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.record_table.currentCellChanged.connect(self._on_row_changed)
        left_layout.addWidget(self.record_table, 1)
        splitter.addWidget(left)

        # ── 中：图片查看器 ──
        self.image_viewer = ImageViewer(self)
        self.image_viewer.setMinimumWidth(320)
        splitter.addWidget(self.image_viewer)

        # ── 右：证据 / 重判卡（需求 5 三列链路需要更宽，否则判定值被裁切）──
        self.card = WorkbenchCard(self)
        self.card.verdict_changed.connect(self._on_verdict_changed)
        self.card.evidence_selected.connect(self._on_evidence_selected)
        self.card.setMinimumWidth(460)
        splitter.addWidget(self.card)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)
        outer.addWidget(splitter, 1)

        # 底部操作行
        bottom = QHBoxLayout()
        self.hint_label = QLabel(
            "重判为**幂等**操作：以最终一次为准；重导出会反映最终判定。", self
        )
        self.hint_label.setObjectName("reviewHint")
        bottom.addWidget(self.hint_label, 1)
        self.btn_export = QPushButton("保存本轮留痕并重导出", self)
        self.btn_export.setObjectName("primaryButton")
        self.btn_export.clicked.connect(self.export_requested.emit)
        bottom.addWidget(self.btn_export)
        outer.addLayout(bottom)

    # ─────────────────────── 状态标签 ───────────────────────

    @staticmethod
    def _status_text(status: str, count: int) -> str:
        """状态标签文案（``名称 数量``）。"""
        return f"{_STATUS_TEXT.get(status, status)} {count}"

    @staticmethod
    def _status_color(status: str) -> str:
        """状态标签灯色。"""
        if status == _STATUS_ALL:
            return _STATUS_ALL_COLOR
        return VERDICT_COLORS.get(status, Palette.TEXT_WEAK)

    def _style_status_button(self, status: str, *, checked: bool) -> None:
        """按选中态刷新标签按钮样式（选中 = 实心灯色，未选中 = 白底灯色描边）。"""
        color = self._status_color(status)
        if checked:
            background, foreground = color, "#FFFFFF"
        else:
            background, foreground = "#FFFFFF", color
        weight = "font-weight:bold;" if checked else ""
        self.status_buttons[status].setStyleSheet(
            f"QPushButton{{background:{background};color:{foreground};"
            f"border:1px solid {color};border-radius:12px;"
            f"padding:4px 14px;{weight}}}"
        )

    def _update_status_buttons(self) -> None:
        """刷新全部标签的文案（含数量）与样式。"""
        counts = self._verdict_counts()
        for status, btn in self.status_buttons.items():
            if status == _STATUS_ALL:
                count = sum(counts.values())
            else:
                count = counts.get(status, 0)
            btn.setText(self._status_text(status, count))
            self._style_status_button(status, checked=(status == self._status))

    def _verdict_counts(self) -> dict[str, int]:
        """统计**全部结果**（不受过滤影响）的四类数量。"""
        counts = {v.value: 0 for v in Verdict}
        results = self._all_results()
        for result in results:
            verdict = getattr(result, "verdict", None)
            value = getattr(verdict, "value", str(verdict))
            counts[value] = counts.get(value, 0) + 1
        return counts

    def _on_status_clicked(self, status: str) -> None:
        """切换状态标签。"""
        if status == self._status:
            # 重复点击同一标签 → 保持选中（QButtonGroup 独占下不取消选中）
            self.status_buttons[status].setChecked(True)
            return
        self._status = status
        self.reload()

    def _on_search_changed(self, _text: str) -> None:
        """模糊搜索框变化 → 实时过滤（需求 3）。"""
        self.reload()

    # ─────────────────────── 过滤 ───────────────────────

    def _all_results(self) -> list[CheckResult]:
        """全部结果（未过滤）。"""
        if self._session is None:
            return []
        return list(self._session.results())

    def filtered_keys(self) -> list[str]:
        """当前表格中显示的记录 key 顺序（供测试与外部定位）。"""
        return list(self._keys_in_order)

    def _visible_results(self) -> list[CheckResult]:
        """过滤管线：``全部结果 → 状态标签 → 模糊搜索``。"""
        results = self._all_results()
        if self._status != _STATUS_ALL:
            results = [
                r
                for r in results
                if getattr(getattr(r, "verdict", None), "value", "") == self._status
            ]
        keyword = self.search_edit.text().strip().lower()
        if keyword:
            results = [r for r in results if self._matches_keyword(r, keyword)]
        return results

    @staticmethod
    def _matches_keyword(result: CheckResult, keyword: str) -> bool:
        """三字段（出货单号 / 订单号 / 物料编号）大小写不敏感子串匹配。"""
        record = getattr(result, "record", None)
        if record is None:
            return False
        candidates = (
            getattr(record, "ticket_no", "") or "",
            getattr(record, "order_no", "") or "",
            getattr(record, "part_no", "") or "",
        )
        return any(keyword in str(value).lower() for value in candidates)

    # ─────────────────────── 列表刷新 ───────────────────────

    def set_session(self, session: AppSession) -> None:
        """替换会话结果集并刷新。"""
        self._session = session
        self.reload()

    def reload(self) -> None:
        """按当前「状态标签 + 模糊搜索」重建记录表格。"""
        results = self._visible_results()
        selected_key = self.card.current_key()

        self.record_table.blockSignals(True)
        self.record_table.setRowCount(0)
        self._keys_in_order = []
        for result in results:
            self._append_row(result)
            self._keys_in_order.append(result.key)
        self.record_table.blockSignals(False)

        self._update_status_buttons()

        pending = len(self._session.pending_results()) if self._session else 0
        self.pending_label.setText(f"待复核：{pending} 条")

        # 尽量恢复选中
        if selected_key and selected_key in self._keys_in_order:
            self._select_row(self._keys_in_order.index(selected_key))
            return
        if self._keys_in_order:
            self._select_row(0)
        else:
            self.card.clear()
            self.image_viewer.show_message(self._empty_message())
            self.image_viewer.setEnabled(True)

    def _empty_message(self) -> str:
        """空结果提示（区分"无记录"与"过滤后无匹配"）。"""
        if not self._all_results():
            return "（当前没有待复核记录）"
        return "（无匹配记录）"

    def _append_row(self, result: CheckResult) -> None:
        """追加一条记录行（``订单号 | 物料编号 | 核验结果``）。"""
        record = getattr(result, "record", None)
        order = (getattr(record, "order_no", "") if record is not None else "") or "-"
        part = (getattr(record, "part_no", "") if record is not None else "") or "-"
        verdict = result.verdict
        label = VERDICT_LABELS.get(verdict, getattr(verdict, "value", ""))
        reviewed = bool(getattr(result, "manually_reviewed", False))
        verdict_text = f"{'✔ ' if reviewed else ''}● {label}"

        row = self.record_table.rowCount()
        self.record_table.insertRow(row)
        cells = (str(order), str(part), verdict_text)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setToolTip(f"{result.key}\n{VERDICT_LABELS.get(verdict, '')}")
            if col == 2:
                item.setForeground(
                    QColor(VERDICT_COLORS.get(getattr(verdict, "value", ""), Palette.TEXT))
                )
            self.record_table.setItem(row, col, item)
        # key 挂在第一列（供选中同步与测试查询）
        first = self.record_table.item(row, 0)
        if first is not None:
            first.setData(Qt.ItemDataRole.UserRole, result.key)
        self.record_table.setRowHeight(row, 26)

    def row_key(self, row: int) -> str:
        """返回第 ``row`` 行对应的记录 key（越界返回空串）。"""
        if row < 0 or row >= self.record_table.rowCount():
            return ""
        item = self.record_table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    # ─────────────────────── 槽 ───────────────────────

    def _on_row_changed(self, current_row: int, _cc: int, _prev_row: int, _pc: int) -> None:
        """选中行变化 → **同步**刷新中间图片 + 右侧证据卡（需求 4）。"""
        self._sync_detail(current_row)

    def _sync_detail(self, row: int) -> None:
        """按行号刷新中间/右侧（``reload`` 与选中变化共用）。"""
        key = self.row_key(row)
        result = self._session.get(key) if (self._session and key) else None
        if result is None:
            self.card.clear()
            self.image_viewer.clear()
            return
        self.card.load_result(result)
        self._show_first_available_image(result)

    def _show_first_available_image(self, result: CheckResult) -> None:
        """加载第一条「存在」的证据图；🔵 缺图时**不加载**、只显提示。"""
        evidences = list(getattr(result, "evidence_images", []) or [])
        if not evidences and getattr(result, "record", None) is not None:
            evidences = list(getattr(result.record, "evidences", []) or [])
        if not evidences:
            self.image_viewer.show_message("🔵 缺图：该记录无图片证据。\n请「标记待补图」并填写备注。")
            return
        # 有不可达 / 目录不存在 / 文件缺失的显式说明
        first_exists = None
        for ev in evidences:
            if getattr(ev, "exists", False):
                first_exists = ev
                break
            if getattr(ev, "unreachable", False):
                self.image_viewer.show_message(
                    "共享盘不可达，图片暂不可预览。\n恢复连接后重试。"
                )
                return
        if first_exists is None:
            self.image_viewer.show_message(
                "🔵 缺图：证据目录不存在或文件缺失。\n请「标记待补图」并填写备注。"
            )
            return
        # 点 9.2：卡片默认展示「第一条存在图」；切换查看器图片时同步卡片当前图
        first_path = str(getattr(first_exists, "image_path", ""))
        self.card.set_current_image(first_path)
        self.image_viewer.load(first_path)

    def _on_evidence_selected(self, path: str) -> None:
        """点击图号按钮 → 切换左侧查看器 + 同步右侧卡片当前图（点 9.2）。"""
        if path:
            # 右侧证据卡只展示「当前选中图」的 OCR（修复原「整条拼接」缺陷）
            self.card.set_current_image(path)
            self.image_viewer.load(path)

    def _on_verdict_changed(self, key: str, verdict: str, note: str, mark_missing: bool) -> None:
        """卡片保存重判 → 转发信号（真正写回由主窗口的 ReviewStore 完成）。"""
        self.verdict_applied.emit(key, verdict, note, mark_missing)
        # 刷新当前项（判定可能已变）
        self.reload()

    # ─────────────────────── 状态 ───────────────────────

    def current_key(self) -> str:
        """当前选中记录 key。"""
        return self.card.current_key()

    def current_status(self) -> str:
        """当前状态标签取值（``all`` 或 ``Verdict.value``）。"""
        return self._status

    def select_key(self, key: str) -> bool:
        """按 key 选中记录。

        若当前过滤下不可见，自动放宽为「全部 + 清空搜索」后重试（保证外部
        「定位到某条记录」的调用总能成功）。

        Args:
            key: 记录 key。

        Returns:
            ``True`` 表示已选中。
        """
        if not key:
            return False
        if key in self._keys_in_order:
            self._select_row(self._keys_in_order.index(key))
            return True
        self._status = _STATUS_ALL
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.reload()
        if key in self._keys_in_order:
            self._select_row(self._keys_in_order.index(key))
            return True
        return False

    def _select_row(self, row: int) -> None:
        """选中某行并同步中/右区（信号屏蔽，避免重复刷新）。"""
        self.record_table.blockSignals(True)
        self.record_table.setCurrentCell(row, 0)
        self.record_table.selectRow(row)
        self.record_table.blockSignals(False)
        self._sync_detail(row)

    def review_pending_count(self) -> int:
        """待复核（⚠️ + 🔵）条数。"""
        results = self._all_results()
        return sum(1 for r in results if getattr(r, "verdict", None) in REVIEW_VERDICTS)
