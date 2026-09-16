"""Zone 1：数据源面板（ui.widgets.data_source_panel）。

**三区布局的第一区**（架构设计 12.B.1）：用户在这里选定

  * **申报要素 Excel**（可 UNC / 长路径）；
  * **图片根目录**（推荐直接选到票号目录；也支持「年份 + 票号」折叠区）；
  * **票号**（自动识别失败时强制手填，Q9）；
  * **输出目录**（成果产出 / 过程产出，缺省建议值）。

底部一行是**操作按钮区**：开始 / 重新开始 / 暂停 / 继续执行 / 中止 —— 按钮文案与可用性
由 :class:`DataSourcePanel` 的 :meth:`set_state` 依据控制器状态机驱动（架构设计 12.A.4）：

  * 无断点：开始按钮 =「▶ 开始」
  * 有未完成断点：开始按钮 =「↻ 重新开始」，暂停按钮 =「▶ 继续执行」

同时承载**断点红字提示** ``QLabel#breakpointHint``（**禁令 2**，12.A）：仅在「未完成断点」
时显字，颜色 ``BREAKPOINT_RED = "#D32F2F"``，文案严格按模板；非匹配断点绝不显红字。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.run_controller import ControllerState
from infra.config import AppConfig
from ui.styles.palette import BREAKPOINT_RED, Palette

__all__ = ["DataSourcePanel"]

#: 按钮文案（架构设计 12.A.1 逐字指定）
BTN_START = "▶ 开始"
BTN_RESTART = "↻ 重新开始"
BTN_RESUME_RUN = "▶ 继续执行"
BTN_PAUSE = "⏸ 暂停"
BTN_RESUME = "▶ 继续"
BTN_STOP = "⏹ 中止"


class DataSourcePanel(QFrame):
    """数据源面板（第一区）+ 操作按钮区 + 断点提示。

    Signals:
        start_clicked: 「▶ 开始」（无断点时）。
        restart_clicked: 「↻ 重新开始」（有断点时从零重跑）。
        resume_run_clicked: 「▶ 继续执行」（有断点时续跑）。
        pause_clicked: 「⏸ 暂停」。
        resume_clicked: 「▶ 继续」（从暂停恢复）。
        stop_clicked: 「⏹ 中止」。
        probe_clicked: 数据源变化后请求重新探测（预检 / 断点）。
        inputs_changed: 任意输入框内容变化（用于触发探测 / 保存配置）。

    Args:
        config: 应用配置（用于预填输入框与回写）。
        parent: Qt 父控件。
    """

    start_clicked = Signal()
    restart_clicked = Signal()
    resume_run_clicked = Signal()
    pause_clicked = Signal()
    resume_clicked = Signal()
    stop_clicked = Signal()
    probe_clicked = Signal()
    inputs_changed = Signal()

    def __init__(self, config: AppConfig | None = None, parent: QWidget | None = None) -> None:
        """构造数据源面板。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._config = config if config is not None else AppConfig()

        # 当前按钮语义（由状态机驱动）
        self._has_breakpoint = False
        self._running = False
        self._paused = False

        self._build_ui()
        self._wire()
        self._load_from_config()
        # 初始态：无断点、未运行
        self.set_state(ControllerState.IDLE_NO_BREAKPOINT)

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建面板布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        title = QLabel("① 数据源", self)
        title.setObjectName("zoneTitle")
        outer.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        # 申报要素 Excel
        self.excel_edit = QLineEdit(self)
        self.excel_edit.setPlaceholderText("选择申报要素 Excel（支持 UNC / 长路径）")
        self.excel_edit.setToolTip("申报要素 .xlsx 文件路径")
        excel_btn = QPushButton("浏览…", self)
        excel_btn.clicked.connect(self._on_browse_excel)
        grid.addWidget(self._label("申报要素 Excel"), 0, 0)
        grid.addWidget(self.excel_edit, 0, 1)
        grid.addWidget(excel_btn, 0, 2)

        # 图片根目录
        self.share_edit = QLineEdit(self)
        self.share_edit.setPlaceholderText("选择图片根目录（推荐直接选到票号目录）")
        self.share_edit.setToolTip("产品实拍图片所在目录（票号层）")
        share_btn = QPushButton("浏览…", self)
        share_btn.clicked.connect(self._on_browse_share)
        grid.addWidget(self._label("图片根目录"), 1, 0)
        grid.addWidget(self.share_edit, 1, 1)
        grid.addWidget(share_btn, 1, 2)

        # 票号
        self.ticket_edit = QLineEdit(self)
        self.ticket_edit.setPlaceholderText("票号（自动识别失败时请手填，如 SA26090215）")
        self.ticket_edit.setToolTip("出货通知书号；自动识别失败时必须手工填写（Q9）")
        probe_btn = QPushButton("预检 / 探测断点", self)
        probe_btn.clicked.connect(self.probe_clicked.emit)
        grid.addWidget(self._label("票号"), 2, 0)
        grid.addWidget(self.ticket_edit, 2, 1)
        grid.addWidget(probe_btn, 2, 2)

        # 成果产出目录
        self.result_edit = QLineEdit(self)
        self.result_edit.setPlaceholderText("成果产出目录（汇总表 / 复核清单）")
        result_btn = QPushButton("浏览…", self)
        result_btn.clicked.connect(self._on_browse_result)
        grid.addWidget(self._label("成果产出目录"), 3, 0)
        grid.addWidget(self.result_edit, 3, 1)
        grid.addWidget(result_btn, 3, 2)

        # 过程产出目录
        self.process_edit = QLineEdit(self)
        self.process_edit.setPlaceholderText("过程产出目录（JSON 日志 / 断点 / 缓存）")
        process_btn = QPushButton("浏览…", self)
        process_btn.clicked.connect(self._on_browse_process)
        grid.addWidget(self._label("过程产出目录"), 4, 0)
        grid.addWidget(self.process_edit, 4, 1)
        grid.addWidget(process_btn, 4, 2)

        outer.addLayout(grid)

        # 断点提示（红字，仅在未完成断点时显字）
        self.breakpoint_hint = QLabel("", self)
        self.breakpoint_hint.setObjectName("breakpointHint")
        self.breakpoint_hint.setWordWrap(True)
        self.breakpoint_hint.setVisible(False)
        outer.addWidget(self.breakpoint_hint)

        # 预检结果提示（中性色）
        self.status_hint = QLabel("", self)
        self.status_hint.setObjectName("reviewHint")
        self.status_hint.setWordWrap(True)
        outer.addWidget(self.status_hint)

        outer.addLayout(self._build_buttons())

    def _build_buttons(self) -> QHBoxLayout:
        """构建操作按钮行。"""
        row = QHBoxLayout()
        row.setSpacing(8)

        self.btn_start = QPushButton(BTN_START, self)
        self.btn_start.setObjectName("primaryButton")
        self.btn_pause = QPushButton(BTN_PAUSE, self)
        self.btn_stop = QPushButton(BTN_STOP, self)

        self.btn_start.clicked.connect(self._on_primary_clicked)
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)

        row.addWidget(self.btn_start)
        row.addWidget(self.btn_pause)
        row.addStretch(1)
        row.addWidget(self.btn_stop)
        return row

    def _label(self, text: str) -> QLabel:
        """构造字段标签。"""
        lbl = QLabel(text, self)
        lbl.setObjectName("fieldLabel")
        return lbl

    def _wire(self) -> None:
        """接线输入变化信号。"""
        for edit in (
            self.excel_edit,
            self.share_edit,
            self.ticket_edit,
            self.result_edit,
            self.process_edit,
        ):
            edit.textChanged.connect(self.inputs_changed.emit)

    # ─────────────────────── 配置读写 ───────────────────────

    def _load_from_config(self) -> None:
        """用配置预填输入框。"""
        self.excel_edit.setText(getattr(self._config, "excel_path", "") or "")
        self.share_edit.setText(getattr(self._config, "share_root", "") or "")
        self.ticket_edit.setText(getattr(self._config, "ticket_no", "") or "")
        self.result_edit.setText(getattr(self._config, "result_dir", "") or "")
        self.process_edit.setText(getattr(self._config, "process_dir", "") or "")

    def apply_to_config(self) -> AppConfig:
        """把输入框内容回写配置对象并返回（调用方负责 ``.save()``）。"""
        cfg = self._config
        cfg.excel_path = self.excel_edit.text().strip()
        cfg.share_root = self.share_edit.text().strip()
        cfg.ticket_no = self.ticket_edit.text().strip()
        cfg.result_dir = self.result_edit.text().strip()
        cfg.process_dir = self.process_edit.text().strip()
        return cfg

    # ─────────────────────── 浏览槽 ───────────────────────

    def _pick_file(self) -> str:
        """打开文件选择框选 Excel。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择申报要素 Excel", self.excel_edit.text().strip(), "Excel 文件 (*.xlsx *.xls)"
        )
        return path

    def _pick_dir(self, title: str, current: str) -> str:
        """打开目录选择框。"""
        return QFileDialog.getExistingDirectory(self, title, current)

    def _on_browse_excel(self) -> None:
        """浏览 Excel。"""
        picked = self._pick_file()
        if picked:
            self.excel_edit.setText(picked)

    def _on_browse_share(self) -> None:
        """浏览图片根目录。"""
        picked = self._pick_dir("选择图片根目录", self.share_edit.text().strip())
        if picked:
            self.share_edit.setText(picked)

    def _on_browse_result(self) -> None:
        """浏览成果产出目录。"""
        picked = self._pick_dir("选择成果产出目录", self.result_edit.text().strip())
        if picked:
            self.result_edit.setText(picked)

    def _on_browse_process(self) -> None:
        """浏览过程产出目录。"""
        picked = self._pick_dir("选择过程产出目录", self.process_edit.text().strip())
        if picked:
            self.process_edit.setText(picked)

    # ─────────────────────── 按钮点击路由 ───────────────────────

    def _on_primary_clicked(self) -> None:
        """主按钮：有断点 → 重新开始；无断点 → 开始。"""
        if self._has_breakpoint:
            self.restart_clicked.emit()
        else:
            self.start_clicked.emit()

    def _on_pause_clicked(self) -> None:
        """暂停按钮：运行中 → 暂停；暂停中 → 继续。"""
        if self._paused:
            self.resume_clicked.emit()
        else:
            self.pause_clicked.emit()

    # ─────────────────────── 状态机驱动 ───────────────────────

    def set_state(self, state: ControllerState) -> None:
        """依据控制器状态刷新按钮文案与可用性（架构设计 12.A.4）。

        Args:
            state: 控制器当前状态。
        """
        self._running = state in (ControllerState.RUNNING,)
        self._paused = state in (ControllerState.PAUSED,)
        self._has_breakpoint = state in (ControllerState.IDLE_WITH_BREAKPOINT,)

        if state in (ControllerState.IDLE_WITH_BREAKPOINT,):
            self.btn_start.setText(BTN_RESTART)
        elif state in (ControllerState.PAUSED,):
            self.btn_start.setText(BTN_RESUME_RUN)
        else:
            self.btn_start.setText(BTN_START)

        # 主按钮在运行中出现时禁用（用暂停 / 中止控制）
        self.btn_start.setEnabled(
            state
            in (
                ControllerState.IDLE,
                ControllerState.IDLE_NO_BREAKPOINT,
                ControllerState.IDLE_WITH_BREAKPOINT,
                ControllerState.PAUSED,
                ControllerState.ABORTED,
                ControllerState.FINISHED,
                ControllerState.FAILED,
            )
        )

        # 暂停按钮：RUNNING → 「⏸ 暂停」；PAUSED → 「▶ 继续执行」
        if state in (ControllerState.PAUSED,):
            self.btn_pause.setText(BTN_RESUME_RUN)
            self.btn_pause.setEnabled(True)
            self.btn_pause.setObjectName("primaryButton")
        else:
            self.btn_pause.setText(BTN_PAUSE)
            self.btn_pause.setEnabled(state in (ControllerState.RUNNING,))

        self.btn_stop.setEnabled(state in (ControllerState.RUNNING, ControllerState.PAUSED))
        self._refresh_style()

    def _refresh_style(self) -> None:
        """重新应用 QSS（objectName 变化后需要）。"""
        for btn in (self.btn_start, self.btn_pause):
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # ─────────────────────── 提示文案 ───────────────────────

    def show_breakpoint_hint(self, text: str) -> None:
        """显示断点红字提示（**仅在未完成断点时调用**，禁令 2）。

        文案必须由 :meth:`app.events.BreakpointSummary.hint_text` 生成，保证逐字一致。
        """
        if text:
            self.breakpoint_hint.setStyleSheet(f"color: {BREAKPOINT_RED};")
            self.breakpoint_hint.setText(text)
            self.breakpoint_hint.setVisible(True)
        else:
            self.clear_breakpoint_hint()

    def clear_breakpoint_hint(self) -> None:
        """清除断点红字（无未完成断点时，绝不留红字）。"""
        self.breakpoint_hint.setText("")
        self.breakpoint_hint.setVisible(False)

    def show_status(self, text: str, *, error: bool = False) -> None:
        """显示中性提示（预检结果等）。"""
        color = Palette.DANGER if error else Palette.TEXT_WEAK
        self.status_hint.setStyleSheet(f"color: {color};")
        self.status_hint.setText(text or "")

    # ─────────────────────── 输入快照 ───────────────────────

    def inputs(self) -> dict[str, str]:
        """返回输入框当前值快照。"""
        return {
            "excel_path": self.excel_edit.text().strip(),
            "share_root": self.share_edit.text().strip(),
            "ticket_no": self.ticket_edit.text().strip(),
            "result_dir": self.result_edit.text().strip(),
            "process_dir": self.process_edit.text().strip(),
        }

    def set_ticket_no(self, ticket: str) -> None:
        """写回自动识别到的票号（不改动用户已手填的其它字段）。"""
        if ticket:
            self.ticket_edit.setText(ticket)
