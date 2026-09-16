"""人工复核工作台（ui.widgets.review_workbench）。

把「**左图右证据**」+「**记录列表**」+「**重判/备注**」+「**复核后重导出**」组装为一个
独立可切换的工作台面板（架构设计 12.C）：

  * **左**：待复核记录列表（默认只列 ``REVIEW_VERDICTS`` = ⚠️ + 🔵；可切「全部」）；
  * **中**：:class:`ui.widgets.image_viewer.ImageViewer`（🔵 缺图时**不加载图**，只显提示）；
  * **右**：:class:`ui.widgets.workbench_card.WorkbenchCard`（证据 OCR + 重判 + 备注 + 标记待补图）；
  * **底部**： 「保存本轮留痕并重导出」（``复核留痕/review_round_N.json`` + 覆盖 ``*_复核后.json``）。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑（重判逻辑委托
:class:`app.review_store.ReviewStore`）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.session import AppSession
from core.models import CheckResult
from ui.styles.palette import VERDICT_COLORS, Palette
from ui.widgets.image_viewer import ImageViewer
from ui.widgets.workbench_card import WorkbenchCard

__all__ = ["ReviewWorkbench"]

_FILTER_ALL = "all"
_FILTER_REVIEW = "review"


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
        self._filter = _FILTER_REVIEW
        self._build_ui()
        self.reload()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建工作台布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("人工复核工作台", self)
        title.setObjectName("zoneTitle")
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(QLabel("显示：", self))
        self.filter_combo = QComboBox(self)
        self.filter_combo.addItem("待人工复核（⚠️ + 🔵）", _FILTER_REVIEW)
        self.filter_combo.addItem("全部记录", _FILTER_ALL)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        header.addWidget(self.filter_combo)
        header.addStretch(1)
        self.pending_label = QLabel("待复核：0 条", self)
        self.pending_label.setStyleSheet(f"color: {Palette.WARN}; font-weight: bold;")
        header.addWidget(self.pending_label)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # ── 左：记录列表 ──
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("记录：", self))
        self.record_list = QListWidget(self)
        self.record_list.setMinimumWidth(220)
        self.record_list.currentItemChanged.connect(self._on_record_changed)
        left_layout.addWidget(self.record_list, 1)
        splitter.addWidget(left)

        # ── 中：图片查看器 ──
        self.image_viewer = ImageViewer(self)
        self.image_viewer.setMinimumWidth(320)
        splitter.addWidget(self.image_viewer)

        # ── 右：证据 / 重判卡 ──
        self.card = WorkbenchCard(self)
        self.card.verdict_changed.connect(self._on_verdict_changed)
        self.card.evidence_selected.connect(self._on_evidence_selected)
        self.card.setMinimumWidth(320)
        splitter.addWidget(self.card)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 3)
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

    # ─────────────────────── 列表刷新 ───────────────────────

    def set_session(self, session: AppSession) -> None:
        """替换会话结果集并刷新。"""
        self._session = session
        self.reload()

    def reload(self) -> None:
        """按当前过滤重建记录列表。"""
        results = self._results_for_filter()
        selected_key = self.card.current_key()

        self.record_list.blockSignals(True)
        self.record_list.clear()
        for result in results:
            self.record_list.addItem(self._make_item(result))
        self.record_list.blockSignals(False)

        pending = len(self._session.pending_results()) if self._session else 0
        self.pending_label.setText(f"待复核：{pending} 条")

        # 尽量恢复选中
        if selected_key:
            for row in range(self.record_list.count()):
                item = self.record_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == selected_key:
                    self.record_list.setCurrentRow(row)
                    return
        if self.record_list.count() > 0:
            self.record_list.setCurrentRow(0)
        else:
            self.card.clear()
            self.image_viewer.show_message("（当前没有待复核记录）")

    def _results_for_filter(self) -> list[CheckResult]:
        """返回当前过滤下的记录列表。"""
        if self._session is None:
            return []
        if self._filter == _FILTER_ALL:
            return list(self._session.results())
        return list(self._session.pending_results())

    def _make_item(self, result: CheckResult) -> QListWidgetItem:
        """构造一条列表项（着色 + 记录 key）。"""
        record = getattr(result, "record", None)
        part = getattr(record, "part_no", "") if record is not None else ""
        order = getattr(record, "order_no", "") if record is not None else ""
        ticket = getattr(record, "ticket_no", "") if record is not None else ""
        label = f"{result.verdict.value}\n{ticket or '-'} | {part or '-'} | {order or '-'}"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, result.key)
        color = VERDICT_COLORS.get(result.verdict.value, Palette.TEXT)
        from PySide6.QtGui import QColor

        item.setForeground(QColor(color))
        if getattr(result, "manually_reviewed", False):
            item.setText(f"✔ {label}")
        return item

    # ─────────────────────── 槽 ───────────────────────

    def _on_filter_changed(self, _index: int) -> None:
        """切换过滤。"""
        self._filter = str(self.filter_combo.currentData() or _FILTER_REVIEW)
        self.reload()

    def _on_record_changed(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        """选中记录变化 → 刷新卡片 + 加载图片。"""
        if current is None:
            self.card.clear()
            self.image_viewer.clear()
            return
        key = str(current.data(Qt.ItemDataRole.UserRole) or "")
        result = self._session.get(key) if self._session else None
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
        self.image_viewer.load(str(getattr(first_exists, "image_path", "")))

    def _on_evidence_selected(self, path: str) -> None:
        """点击图号按钮 → 切换左侧查看器。"""
        if path:
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

    def select_key(self, key: str) -> None:
        """按 key 选中记录。"""
        for row in range(self.record_list.count()):
            item = self.record_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                self.record_list.setCurrentRow(row)
                return
