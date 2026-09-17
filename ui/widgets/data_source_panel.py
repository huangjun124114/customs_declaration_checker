"""Zone 1：数据源面板（ui.widgets.data_source_panel）。

**三区布局的第一区**（架构设计 12.B.1 + v0.2.0 点 2/3）。用户在这里选定：

  * **申报要素 Excel**（可 UNC / 长路径）—— 必填；
  * **图片根目录**（推荐直接选到票号目录）—— 必填；
  * **票号**（**只读展示**自动 / 推断结果；右侧附「**改**」纠正入口 —— P2-2：
    目录推断可能误把纯数字目录名当票号，用户需有可逆的纠正通道；失败时由
    ``main_window`` 兜底弹框手填，Q2）。

⚠️ **v0.2.0 点 2 —— 控件精简**：``QGridLayout`` 由 5 行减为 **2 行（+1 行票号只读展示）**：
「成果产出目录」「过程产出目录」两行**整行移除**，改由运行目录约定自动就位
（``app_base_dir()/报关申报要素校验/{result,logs}``，见 :class:`app.path_policy.PathPolicy`）。
``inputs()`` 仍返回**原有 5 键字典**，但 ``result_dir`` / ``process_dir`` 改为**派生值**
（不再从控件读取），以兼容既有调用方。

底部一行是**操作按钮区**：开始 / 重新开始 / 暂停 / 继续执行 / 中止 —— 按钮文案与可用性
由 :meth:`DataSourcePanel.set_state` 依据控制器状态机驱动（架构设计 12.A.4）：

  * 无断点：开始按钮 =「▶ 开始」
  * 有未完成断点：开始按钮 =「↻ 重新开始」，暂停按钮 =「▶ 继续执行」

⚠️ **v0.2.0 点 3 —— 运行期锁定数据源**：``RUNNING`` / ``PAUSED`` 期间，Excel 输入框 +
浏览按钮、图片根输入框 + 浏览按钮、预检按钮**全部置灰**。``PAUSED`` 也锁定是**刻意的**：
断点已绑定当前数据源指纹（``Fingerprint.is_same_source``），暂停期间换源会导致串票。

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
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.path_policy import PathPolicy
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
        path_policy: 路径策略（缺省新建；可注入以控制派生输出目录，主要供测试）。
    """

    start_clicked = Signal()
    restart_clicked = Signal()
    resume_run_clicked = Signal()
    pause_clicked = Signal()
    resume_clicked = Signal()
    stop_clicked = Signal()
    probe_clicked = Signal()
    inputs_changed = Signal()

    def __init__(
        self,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
        *,
        path_policy: PathPolicy | None = None,
    ) -> None:
        """构造数据源面板。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._config = config if config is not None else AppConfig()
        self._path_policy = path_policy if path_policy is not None else PathPolicy()

        # 当前按钮语义（由状态机驱动）
        self._has_breakpoint = False
        self._running = False
        self._paused = False

        # 派生的输出目录（v0.2.0 运行目录约定；控件精简后不再由用户选择）
        self._result_dir: str = ""
        self._process_dir: str = ""

        self._build_ui()
        self._wire()
        self._resolve_output_dirs()
        self._load_from_config()
        # 初始态：无断点、未运行
        self.set_state(ControllerState.IDLE_NO_BREAKPOINT)

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建面板布局（v0.2.0：5 行 → 2 个可编辑行 + 1 个票号只读行）。"""
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

        # ── 第 0 行：申报要素 Excel（保留，可编辑）──
        self.excel_edit = QLineEdit(self)
        self.excel_edit.setPlaceholderText("选择申报要素 Excel（支持 UNC / 长路径）")
        self.excel_edit.setToolTip("申报要素 .xlsx 文件路径")
        self.excel_btn = QPushButton("浏览…", self)
        self.excel_btn.clicked.connect(self._on_browse_excel)
        grid.addWidget(self._label("申报要素 Excel"), 0, 0)
        grid.addWidget(self.excel_edit, 0, 1)
        grid.addWidget(self.excel_btn, 0, 2)

        # ── 第 1 行：图片根目录（保留，可编辑）──
        self.share_edit = QLineEdit(self)
        self.share_edit.setPlaceholderText("选择图片根目录（推荐直接选到票号目录）")
        self.share_edit.setToolTip("产品实拍图片所在目录（票号层）")
        self.share_btn = QPushButton("浏览…", self)
        self.share_btn.clicked.connect(self._on_browse_share)
        grid.addWidget(self._label("图片根目录"), 1, 0)
        grid.addWidget(self.share_edit, 1, 1)
        grid.addWidget(self.share_btn, 1, 2)

        # ── 第 2 行：票号（只读展示 + 「改」纠正入口；失败时由 main_window 兜底弹框手填，Q2）──
        self.ticket_edit = QLineEdit(self)
        self.ticket_edit.setPlaceholderText("预检识别 / 目录推断的票号（识别失败会提示手填）")
        self.ticket_edit.setToolTip("出货通知书号（只读；自动识别失败时由程序兜底获取）")
        self.ticket_edit.setReadOnly(True)
        self.ticket_edit.setFrame(False)
        self.ticket_edit.setObjectName("readonlyField")
        # P2-2：目录推断可能误把纯数字目录名（如 20260902）当票号 → 提供轻量纠正入口，
        # 避免「推断错 → 用户没注意 → 静默跑错票」。改完刷新标签，后续「预检」仍会
        # 重新识别并覆盖（不做不可逆写死）。
        self.ticket_edit_btn = QPushButton("改", self)
        self.ticket_edit_btn.setObjectName("ticketEditButton")
        self.ticket_edit_btn.setToolTip("手动修改票号（目录推断可能误判为纯数字目录名）")
        self.ticket_edit_btn.setMaximumWidth(40)
        self.ticket_edit_btn.clicked.connect(self._on_edit_ticket)
        self.probe_btn = QPushButton("预检 / 探测断点", self)
        self.probe_btn.clicked.connect(self.probe_clicked.emit)
        grid.addWidget(self._label("票号"), 2, 0)
        grid.addWidget(self.ticket_edit, 2, 1)
        grid.addWidget(self.ticket_edit_btn, 2, 2)
        grid.addWidget(self.probe_btn, 2, 3)

        # 注：v0.2.0 起「成果产出目录」「过程产出目录」两行**整行移除** —— 输出目录
        # 改由运行目录约定（app_base_dir()/报关申报要素校验/{result,logs}）自动就位。

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
        """接线输入变化信号（票号只读，不参与 inputs_changed）。

        ⚠️ **必须经一层 Python 槽转发**，不能写成 ``connect(self.inputs_changed.emit)``：

        ``QLineEdit.textChanged`` **只有 ``(str)`` 一个重载**，而 ``Signal.emit``
        的形参是 ``*args`` → PySide6 无法据此截断参数，会把文本原样传给 emit →
        ``TypeError: inputs_changed() only accepts 0 argument(s), 1 given!``
        （实测：手工输入 / 粘贴 / 清空路径时 stderr 每次都抛，信号**从未发出**）。

        反例警告：``QPushButton.clicked`` **恰好能用**同样的写法 —— 因为它有
        ``clicked()`` / ``clicked(bool)`` 双重载，PySide6 会自动挑 0 参那个。
        **别照抄按钮的写法。**
        """
        for edit in (self.excel_edit, self.share_edit):
            edit.textChanged.connect(self._on_edit_changed)

    def _on_edit_changed(self, _text: str) -> None:
        """输入框内容变化 → 转成无参 ``inputs_changed``（丢弃文本，只做"变了"通知）。"""
        self.inputs_changed.emit()

    # ─────────────────────── 输出目录（派生）───────────────────────

    def _resolve_output_dirs(self) -> None:
        """按运行目录约定派生 ``result_dir`` / ``process_dir``（幂等创建，失败静默降级）。"""
        try:
            result_dir, process_dir = self._path_policy.resolve_outputs()
        except Exception:  # noqa: BLE001 - 派生失败不得阻断 UI 构建
            return
        self._result_dir = str(result_dir)
        self._process_dir = str(process_dir)

    def set_output_dirs(
        self,
        result_dir: str,
        process_dir: str,
    ) -> None:
        """覆盖派生输出目录（供测试 / 特殊部署注入；不影响正常约定路径）。

        Args:
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
        """
        self._result_dir = str(result_dir)
        self._process_dir = str(process_dir)

    def output_dirs(self) -> tuple[str, str]:
        """返回当前派生的 ``(result_dir, process_dir)``。"""
        return self._result_dir, self._process_dir

    # ─────────────────────── 配置读写 ───────────────────────

    def _load_from_config(self) -> None:
        """用配置预填可编辑输入框（输出目录不再来自配置，见 Q8）。"""
        self.excel_edit.setText(getattr(self._config, "excel_path", "") or "")
        self.share_edit.setText(getattr(self._config, "share_root", "") or "")
        self.ticket_edit.setText(getattr(self._config, "ticket_no", "") or "")

    def apply_to_config(self) -> AppConfig:
        """把输入框内容回写配置对象并返回（调用方负责 ``.save()``）。

        ⚠️ v0.2.0：**不写回** ``result_dir`` / ``process_dir``（Q8：字段降级为兼容占位）。
        """
        cfg = self._config
        cfg.excel_path = self.excel_edit.text().strip()
        cfg.share_root = self.share_edit.text().strip()
        cfg.ticket_no = self.ticket_edit.text().strip()
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
        """依据控制器状态刷新按钮文案与可用性（架构设计 12.A.4 + v0.2.0 点 3）。

        除按钮语义外，还管控**输入可用性**：``RUNNING`` / ``PAUSED`` 期间锁定数据源
        （Excel 输入框 + 浏览按钮 / 图片根输入框 + 浏览按钮 / 预检按钮 全部置灰）。

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

        # ── v0.2.0 点 3：运行期锁定数据源 ──
        locked = state in (ControllerState.RUNNING, ControllerState.PAUSED)
        for widget in (
            self.excel_edit,
            self.excel_btn,
            self.share_edit,
            self.share_btn,
            self.probe_btn,
            self.ticket_edit_btn,
        ):
            widget.setEnabled(not locked)

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
        """返回输入快照（**恒为 5 键**，调用方依赖）。

        ``result_dir`` / ``process_dir`` 为**派生值**（运行目录约定），不再是控件内容。
        """
        return {
            "excel_path": self.excel_edit.text().strip(),
            "share_root": self.share_edit.text().strip(),
            "ticket_no": self.ticket_edit.text().strip(),
            "result_dir": self._result_dir,
            "process_dir": self._process_dir,
        }

    def set_ticket_no(self, ticket: str) -> None:
        """写回票号（自动识别 / 目录推断 / 兜底手填的结果）。"""
        if ticket:
            self.ticket_edit.setText(ticket)

    def _on_edit_ticket(self) -> None:
        """「改」纠正入口（P2-2）：弹输入框手动修改票号。

        背景：票号现为**只读标签**，初始值来自「图片根目录末段推断」；若误选目录层级，
        推断值可能是纯数字目录名（如 ``20260902``），用户此前看不到纠正通道。
        此处提供轻量纠正入口：改完刷新标签并发 ``inputs_changed``（回写配置）；后续
        「预检」仍会重新识别并覆盖，故纠正是**可逆**的。
        """
        current = self.ticket_edit.text().strip()
        text, ok = QInputDialog.getText(
            self,
            "修改票号",
            "请输入正确的出货通知书号（票号）。\n"
            "提示：留空或取消不会改动；「预检」仍会重新识别并覆盖此值。",
            QLineEdit.EchoMode.Normal,
            current,
        )
        if ok and text and text.strip():
            self.set_ticket_no(text.strip())
            self.inputs_changed.emit()
