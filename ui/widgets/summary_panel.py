"""Zone 3：结果总览面板（ui.widgets.summary_panel）。

**三区布局的第三区**（架构设计 12.B.3）：

  * **四张计数卡**：✅ 校验合格 / ❌ 校验异常 / ⚠️ 缺图内标识，人工复核 / 🔵 缺图，人工复核；
  * 四卡数值之和 **恒等于**「已处理条数」（FR-006：口径自洽，杜绝「少算 / 重算」）；
  * **「待人工复核」**汇总（``REVIEW_VERDICTS`` = ⚠️ + 🔵），并提供「进入复核工作台」入口；
  * 跑批结束后显示**三产物落点**（成果产出 / 过程产出两类）。

⚠️ **键口径**：计数键恒为判定**枚举短值**（``"PASS"/"FAIL"/"NO_MARK"/"NO_IMAGE"``，
与 ``AppSession.counts()`` / ``PipelineProgress.counts`` 一致）；展示字符串经
``core.constants.VERDICT_TEXT`` 映射，避免「中文展示串当键」的错配。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.run_controller import ControllerState
from core.constants import ALL_VERDICTS, REVIEW_VERDICTS, VERDICT_TEXT
from core.models import Verdict
from ui.styles.palette import VERDICT_COLORS, Palette

__all__ = ["SummaryPanel"]

#: 四卡顺序 + 枚举短值键（与 AppSession.counts() 键一致）
_ORDER: tuple[str, ...] = tuple(v.value for v in ALL_VERDICTS)

#: 枚举短值 → 展示字符串
_TEXT_BY_VALUE: dict[str, str] = {v.value: VERDICT_TEXT[v] for v in Verdict}

#: 「待人工复核」枚举短值集合
_REVIEW_VALUES: frozenset[str] = frozenset(v.value for v in REVIEW_VERDICTS)

#: 允许进入人工复核工作台的**终态**（v0.2.0 点 4）。
#:
#: * ``FINISHED`` —— 跑完；
#: * ``ABORTED`` / ``FAILED`` —— **只要已产出结果**（Q4-附：中止后允许进工作台）。
#: 其余状态（初始 / ``IDLE*`` / ``RUNNING`` / ``PAUSED``）一律**不允许**。
_WORKBENCH_ALLOWED_STATES: frozenset[ControllerState] = frozenset(
    {
        ControllerState.FINISHED,
        ControllerState.ABORTED,
        ControllerState.FAILED,
    }
)


class _CountCard(QFrame):
    """单张计数卡（数值 + 标题，按判定着色）。

    Args:
        verdict_value: 判定**枚举短值**（``"PASS"`` 等）。
        parent: Qt 父控件。
    """

    def __init__(self, verdict_value: str, parent: QWidget | None = None) -> None:
        """构造计数卡。"""
        super().__init__(parent)
        self.setObjectName("countCard")
        self.verdict_value = verdict_value
        display = _TEXT_BY_VALUE.get(verdict_value, verdict_value)
        color = VERDICT_COLORS.get(display, Palette.TEXT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self.value_label = QLabel("0", self)
        self.value_label.setObjectName("countCardValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_label.setStyleSheet(f"color: {color};")

        self.title_label = QLabel(display, self)
        self.title_label.setObjectName("countCardTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)

        layout.addWidget(self.value_label)
        layout.addWidget(self.title_label)

    def set_value(self, value: int) -> None:
        """更新数值。"""
        self.value_label.setText(str(int(value)))


class SummaryPanel(QFrame):
    """结果总览面板（第三区）。

    Signals:
        open_workbench: 「进入复核工作台」被点击。

    Args:
        parent: Qt 父控件。
    """

    open_workbench = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造结果总览面板。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._cards: dict[str, _CountCard] = {}
        #: 最近一次「待人工复核」条数（按钮可用性输入之一）
        self._review_total: int = 0
        #: 最近一次控制器状态（按钮可用性输入之一；``None`` = 尚未收到状态）
        self._run_state: ControllerState | None = None
        self._build_ui()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建面板布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        title = QLabel("③ 结果总览", self)
        title.setObjectName("zoneTitle")
        outer.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for col, verdict_value in enumerate(_ORDER):
            card = _CountCard(verdict_value, self)
            self._cards[verdict_value] = card
            grid.addWidget(card, 0, col)
            grid.setColumnStretch(col, 1)
        outer.addLayout(grid)

        # 行：已处理 + 待复核 + 入口按钮
        info_row = QHBoxLayout()
        self.processed_label = QLabel("已处理：0/0 条", self)
        self.processed_label.setStyleSheet(f"color: {Palette.TEXT}; font-weight: bold;")
        self.review_label = QLabel("待人工复核：0 条", self)
        self.review_label.setStyleSheet(f"color: {Palette.WARN}; font-weight: bold;")
        self.btn_workbench = QPushButton("进入人工复核工作台 →", self)
        self.btn_workbench.clicked.connect(self.open_workbench.emit)
        info_row.addWidget(self.processed_label)
        info_row.addSpacing(16)
        info_row.addWidget(self.review_label)
        info_row.addStretch(1)
        info_row.addWidget(self.btn_workbench)
        outer.addLayout(info_row)

        # 三产物落点
        self.artifacts_label = QLabel("三产物：尚未产出", self)
        self.artifacts_label.setObjectName("reviewHint")
        self.artifacts_label.setWordWrap(True)
        self.artifacts_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self.artifacts_label)

    # ─────────────────────── 更新 ───────────────────────

    def apply_counts(self, counts: dict[str, int], *, processed: int, total: int) -> None:
        """更新四卡数值 + 已处理 + 待复核。

        Args:
            counts: 判定**枚举短值** → 条数（``{"PASS": 15, ...}``）。
            processed: 已处理条数。
            total: 总条数。
        """
        review_total = 0
        for verdict_value, card in self._cards.items():
            value = int(counts.get(verdict_value, 0))
            card.set_value(value)
            if verdict_value in _REVIEW_VALUES:
                review_total += value

        self._review_total = review_total
        self.processed_label.setText(f"已处理：{int(processed)}/{int(total)} 条")
        self.review_label.setText(f"待人工复核：{review_total} 条")
        self._refresh_workbench_button()

    def set_run_state(self, state: ControllerState) -> None:
        """记录控制器状态并刷新工作台入口可用性（v0.2.0 点 4）。

        修复既有缺陷：旧实现仅按 ``review_total > 0`` 判定 —— 运行中 counts 一旦增长，
        按钮就会亮起、用户可点进工作台。现改为「**有待复核项 且 处于终态**」双重条件。

        Args:
            state: 控制器当前状态。
        """
        self._run_state = state
        self._refresh_workbench_button()

    def _refresh_workbench_button(self) -> None:
        """按「有待复核项 且 允许状态」刷新工作台入口按钮可用性。"""
        enabled = self._review_total > 0 and self._run_state in _WORKBENCH_ALLOWED_STATES
        self.btn_workbench.setEnabled(enabled)

    def clear(self) -> None:
        """清零（新一轮跑批前调用）。"""
        for card in self._cards.values():
            card.set_value(0)
        self._review_total = 0
        self.processed_label.setText("已处理：0/0 条")
        self.review_label.setText("待人工复核：0 条")
        self._refresh_workbench_button()
        self.artifacts_label.setText("三产物：尚未产出")

    def show_artifacts(self, output_paths: dict[str, str]) -> None:
        """显示三产物落点。

        Args:
            output_paths: ``{"summary"|"review_csv"|"review"|"detail_json"|"detail": 路径}``。
        """
        if not output_paths:
            self.artifacts_label.setText("三产物：尚未产出")
            return
        lines = ["三产物已落盘："]
        labels = {
            "summary": "汇总表",
            "review_csv": "复核清单",
            "review": "复核清单",
            "detail_json": "详细日志(JSON)",
            "detail": "详细日志(JSON)",
        }
        seen = set()
        for key in ("summary", "review_csv", "review", "detail_json", "detail"):
            path = output_paths.get(key, "")
            if path and key not in seen:
                seen.add(key)
                lines.append(f"  · {labels.get(key, key)}：{path}")
        self.artifacts_label.setText("\n".join(lines))
