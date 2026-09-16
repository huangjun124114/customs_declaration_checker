"""主窗口（ui.main_window）。

**T05 完整实现**：在 T01 骨架（标题栏 / 菜单栏 / 状态栏 / ``closeEvent`` 安全退出）基础上，
填充**三区布局** + **人工复核工作台** + **后台线程编排接线**（架构设计 12.B / 12.C）：

  * **Zone 1（数据源 + 操作按钮 + 断点红字）** → :class:`ui.widgets.data_source_panel.DataSourcePanel`
  * **Zone 2（进度条 + N/M 条 + 日志级别过滤）** → :class:`ui.widgets.progress_log_panel.ProgressLogPanel`
  * **Zone 3（四卡计数 + 待复核 + 三产物落点）** → :class:`ui.widgets.summary_panel.SummaryPanel`
  * **复核工作台（左图右证据 + 重判 + 重导出）** → :class:`ui.widgets.review_workbench.ReviewWorkbench`

接线（第 5 节「后台线程模型」）：UI 通过 :class:`ui.worker.QtRunController` 的**信号**接收进度 /
状态 / 结束事件；**绝不在 UI 线程做跑批**。

**禁令 2（断点 UI，12.A）**：

  * 无断点：开始按钮 =「▶ 开始」；
  * 有未完成断点：开始按钮 =「↻ 重新开始」、暂停按钮 =「▶ 继续执行」、红字提示（``#D32F2F``）；
  * 非匹配断点：**不显红字**，仅写 INFO 日志。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.events import BreakpointSummary, PipelineOutcome, TaskProgress
from app.path_policy import PreflightReport, infer_ticket_no_from_path
from app.review_store import ReviewStore
from app.run_controller import ControllerState
from app.session import AppSession
from infra.config import AppConfig
from infra.logger import LogLevel, Phase, get_logger
from ui.widgets.data_source_panel import DataSourcePanel
from ui.widgets.progress_log_panel import ProgressLogPanel
from ui.widgets.review_workbench import ReviewWorkbench
from ui.widgets.summary_panel import SummaryPanel

__all__ = ["MainWindow"]

APP_TITLE = "报关申报要素自动校验工具"
APP_VERSION = "0.2.0"

#: 「人工复核工作台」页签索引（v0.2.0 点 4：初始隐藏，跑完 / 有结果后显示）
_WORKBENCH_TAB_INDEX = 1


class MainWindow(QMainWindow):
    """主窗口（三区 + 复核工作台 + 后台线程编排）。

    Attributes:
        config: 应用配置（由 ``main.py`` 注入）。
        log: 阶段日志器（``Phase.APP``）。
        session: 会话结果集（跨区共享）。
    """

    def __init__(self, config: AppConfig | None = None, parent: QWidget | None = None) -> None:
        """构造主窗口。

        Args:
            config: 应用配置；缺省读取持久化配置（E1 路径记忆）。
            parent: 父窗口（通常为 ``None``）。
        """
        super().__init__(parent)
        self.config: AppConfig = config if config is not None else AppConfig()
        self.log = get_logger(Phase.APP)
        self.session = AppSession()
        self._controller: Any = None  # QtRunController（延迟创建，避免无 Qt 环境崩）
        self._review_store: ReviewStore | None = None
        self._last_outcome: PipelineOutcome | None = None
        self._last_breakpoint: BreakpointSummary = BreakpointSummary()
        self._last_state: ControllerState = ControllerState.IDLE

        self._ensure_run_dirs()
        self._setup_window()
        self._setup_menu()
        self._setup_central()
        self._setup_status_bar()
        self._wire_controller()

    # ─────────────────────── 初始化 ───────────────────────

    def _ensure_run_dirs(self) -> None:
        """启动即确保运行目录就位（v0.2.0 点 1）。

        双击 exe（或开发态运行）后**立即**生成 ``报关申报要素校验\\{result,logs}``；
        已存在则复用，**绝不清理已有内容**。失败仅记日志，不阻断启动。
        """
        try:
            from app.path_policy import PathPolicy

            result_dir, process_dir = PathPolicy().resolve_outputs()
            self.log.info(f"运行目录就绪：成果={result_dir} 过程={process_dir}")
        except Exception as exc:  # noqa: BLE001 - 目录初始化失败不得阻断启动
            self.log.warn(f"运行目录初始化失败（已忽略）：{exc}")

    def _setup_window(self) -> None:
        """设置窗口基本属性（标题 / 尺寸 / 居中）。"""
        self.setWindowTitle(f"{APP_TITLE}  v{APP_VERSION}")
        self.resize(1280, 820)
        self.setMinimumSize(1024, 680)

    def _setup_menu(self) -> None:
        """构建菜单栏（含「重载规则文件」）。"""
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu("文件(&F)")
        export_action = QAction("保存本轮留痕并重导出(&E)", self)
        export_action.setStatusTip("把人工复核结果写回并覆盖三产物（成果产出）")
        export_action.triggered.connect(self._on_export_reviewed)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        exit_action = QAction("退出(&X)", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.setStatusTip("退出程序")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # 规则菜单（v1.1 C.3：手动重载，不自动热加载）
        rules_menu = menubar.addMenu("规则(&R)")
        reload_action = QAction("重载规则文件(&L)", self)
        reload_action.setStatusTip("重新加载 rules/*.yaml 规则（下次跑批生效）")
        reload_action.triggered.connect(self._on_reload_rules)
        rules_menu.addAction(reload_action)

        # 帮助菜单
        help_menu = menubar.addMenu("帮助(&H)")
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _setup_central(self) -> None:
        """构建中央区：三区 + 复核工作台（``QTabWidget`` 切换）。"""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 三区并排（水平分割：数据源 | 进度日志；下方结果总览）
        self.data_source_panel = DataSourcePanel(self.config, central)
        self.progress_panel = ProgressLogPanel(central)
        self.summary_panel = SummaryPanel(central)
        self.workbench = ReviewWorkbench(self.session, central)

        # 主视图（三区）与工作台用 Tab 切换
        self.tabs = QTabWidget(central)
        self.tabs.addTab(self._build_three_zone(central), "校验主视图")
        self.tabs.addTab(self.workbench, "人工复核工作台")
        # v0.2.0 点 4：初始隐藏工作台页签（跑完 / 有结果后才显示）
        self.tabs.setTabVisible(_WORKBENCH_TAB_INDEX, False)
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)

    def _build_three_zone(self, parent: QWidget) -> QWidget:
        """组装「数据源 / 进度日志 / 结果总览」三区。"""
        page = QWidget(parent)
        vbox = QVBoxLayout(page)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(8)
        vbox.addWidget(self.data_source_panel)
        vbox.addWidget(self.progress_panel, 1)
        vbox.addWidget(self.summary_panel)
        return page

    def _setup_status_bar(self) -> None:
        """构建状态栏。"""
        status: QStatusBar = self.statusBar()
        status.showMessage(f"就绪 · {APP_TITLE} v{APP_VERSION}")

    # ─────────────────────── 控制器接线 ───────────────────────

    def _wire_controller(self) -> None:
        """创建 Qt 控制器并接线三区信号（延迟 import，保证无 Qt 环境可导入本模块）。"""
        try:
            from ui.worker import QtRunController
        except Exception as exc:  # noqa: BLE001 - headless 环境无 Qt 时优雅降级
            self.log.warn("Qt 控制器不可用（无 GUI 环境）：%s", exc)
            return

        self._controller = QtRunController(session=self.session, parent=self)
        self._controller.signals.progress.connect(self._on_progress)
        self._controller.signals.finished.connect(self._on_finished)
        self._controller.signals.state.connect(self._on_state)
        self._controller.signals.breakpoint.connect(self._on_breakpoint)

        # 数据源面板按钮
        self.data_source_panel.probe_clicked.connect(self._on_probe)
        self.data_source_panel.start_clicked.connect(self._on_start)
        self.data_source_panel.restart_clicked.connect(self._on_restart)
        self.data_source_panel.resume_run_clicked.connect(self._on_resume_run)
        self.data_source_panel.pause_clicked.connect(self._on_pause)
        self.data_source_panel.resume_clicked.connect(self._on_resume)
        self.data_source_panel.stop_clicked.connect(self._on_stop)
        self.data_source_panel.inputs_changed.connect(self._on_inputs_changed)

        # 结果总览 → 复核工作台
        self.summary_panel.open_workbench.connect(lambda: self.tabs.setCurrentWidget(self.workbench))
        # 复核工作台 → 主窗口
        self.workbench.verdict_applied.connect(self._on_verdict_applied)
        self.workbench.export_requested.connect(self._on_export_reviewed)

    # ─────────────────────── 输入 / 预检 ───────────────────────

    def _current_inputs(self) -> dict[str, str]:
        """返回数据源面板当前输入快照。"""
        return self.data_source_panel.inputs()

    def _on_inputs_changed(self) -> None:
        """输入变化：把配置回写内存（真正落盘在探测 / 开始前）。"""
        try:
            self.data_source_panel.apply_to_config()
        except Exception as exc:  # noqa: BLE001 - 输入回写失败不影响继续输入
            self.log.debug("配置回写跳过：%s", exc)

    def _on_probe(self) -> None:
        """「预检 / 探测断点」：先预检，再探断点，按禁令 2 刷新红字。"""
        if self._controller is None:
            self.data_source_panel.show_status("GUI 控制器不可用（无 Qt 环境）", error=True)
            return
        inputs = self._current_inputs()
        ticket = inputs["ticket_no"]
        process_dir = inputs["process_dir"]
        if not inputs["excel_path"] or not inputs["share_root"]:
            self.data_source_panel.show_status(
                "请先选择申报要素 Excel 与图片根目录", error=True
            )
            return
        # 预检（拿票号 / 记录数 / 变体，供断点指纹）
        try:
            from app.path_policy import PathPolicy
            from app.preflight import PreflightRunner
            from core.rule_repository import RuleRepository

            policy = PathPolicy()
            repo = RuleRepository()
            repo.load_all()
            report: PreflightReport = PreflightRunner(repo, policy).run(
                inputs["excel_path"], inputs["share_root"], ticket_no=ticket
            )
        except Exception as exc:  # noqa: BLE001 - 预检失败须给明确提示
            from infra.errors import user_message_of

            self.data_source_panel.show_status(user_message_of(exc), error=True)
            self.progress_panel.append_log(user_message_of(exc), level=LogLevel.ERROR.value)
            return

        if report.ticket_hint and not ticket:
            self.data_source_panel.set_ticket_no(report.ticket_hint)
            ticket = report.ticket_hint

        self.data_source_panel.show_status(report.status_text(), error=not report.ok)
        self.progress_panel.append_log(report.status_text(), level=LogLevel.INFO.value)

        # 探测断点（process_dir 为运行目录约定派生的 logs 目录，恒非空；仍保守判空）
        if report.ok and process_dir:
            self._controller.probe_breakpoint(
                ticket_no=ticket or report.ticket_hint,
                excel_path=inputs["excel_path"],
                share_root=inputs["share_root"],
                process_dir=process_dir,
                variant=report.variant,
                total_count=report.record_estimate,
            )

    # ─────────────────────── 开始 / 重跑 / 续跑 ───────────────────────

    def _ready_to_run(self) -> dict[str, str] | None:
        """校验输入齐全后返回输入快照，否则给出提示并返回 ``None``。

        v0.2.0 点 2：必填项由 4 项减为 **2 项**（仅「申报要素 Excel」+「图片根目录」）；
        票号与输出目录由程序兜底（输出目录 = 运行目录约定，票号 = 推断 / 弹框，见
        :meth:`_resolve_ticket_no`）。
        """
        if self._controller is None:
            self.data_source_panel.show_status("GUI 控制器不可用（无 Qt 环境）", error=True)
            return None
        inputs = self._current_inputs()
        missing = [
            name
            for name, key in (
                ("申报要素 Excel", "excel_path"),
                ("图片根目录", "share_root"),
            )
            if not inputs[key]
        ]
        if missing:
            self.data_source_panel.show_status("请先填写：" + "、".join(missing), error=True)
            return None
        # 回写配置（E1 路径记忆）
        try:
            self.data_source_panel.apply_to_config().save()
        except Exception as exc:  # noqa: BLE001 - 配置落盘失败不阻断跑批
            self.log.debug("配置落盘跳过：%s", exc)
        return inputs

    def _resolve_ticket_no(self, inputs: dict[str, str]) -> str:
        """确保票号可用（Q2 兜底）：已填 → 目录末段推断 → 弹框手填 → 允许留空。

        **绝不**以「票号缺失」报错阻止开始（预检仍会再尝试识别）。

        Args:
            inputs: 输入快照（会被就地更新 ``ticket_no``）。

        Returns:
            最终采用的票号（可能为空串）。
        """
        ticket = (inputs.get("ticket_no") or "").strip()
        if ticket:
            return ticket

        inferred = infer_ticket_no_from_path(inputs.get("share_root", ""))
        if inferred:
            self.data_source_panel.set_ticket_no(inferred)
            self.progress_panel.append_log(
                f"已从图片根目录末段推断票号：{inferred}", level=LogLevel.INFO.value
            )
            return inferred

        typed = self._prompt_ticket_no(inputs.get("share_root", ""))
        if typed:
            self.data_source_panel.set_ticket_no(typed)
            return typed
        return ""

    def _prompt_ticket_no(self, share_root: str) -> str:
        """票号推断失败时的兜底输入框（Q2 (a)）。

        Args:
            share_root: 当前图片根目录（用于提示文案）。

        Returns:
            用户填写的票号；取消或留空返回空串（不阻止开始）。
        """
        text, ok = QInputDialog.getText(
            self,
            "请填写票号",
            "未能自动识别票号，且无法从「图片根目录末段」推断。\n"
            f"图片根：{share_root}\n\n"
            "请手工填写票号（如 SA26090215）；留空亦可，预检会再尝试识别：",
        )
        return text.strip() if (ok and text) else ""

    def _launch(self, method_name: str) -> None:
        """统一启动入口（start / restart / resume_run）。"""
        inputs = self._ready_to_run()
        if inputs is None:
            return
        inputs["ticket_no"] = self._resolve_ticket_no(inputs)
        self._reset_runtime_views()
        method = getattr(self._controller, method_name)
        method(
            excel_path=inputs["excel_path"],
            share_root=inputs["share_root"],
            result_dir=inputs["result_dir"],
            process_dir=inputs["process_dir"],
            ticket_no=inputs["ticket_no"],
        )

    def _on_start(self) -> None:
        """「▶ 开始」。"""
        self._launch("start")

    def _on_restart(self) -> None:
        """「↻ 重新开始」（清断点）。"""
        self._launch("restart")

    def _on_resume_run(self) -> None:
        """「▶ 继续执行」（读断点续跑）。"""
        self._launch("resume_run")

    def _on_pause(self) -> None:
        """「⏸ 暂停」。"""
        if self._controller is not None:
            self._controller.pause()

    def _on_resume(self) -> None:
        """「▶ 继续」。"""
        if self._controller is not None:
            self._controller.resume()

    def _on_stop(self) -> None:
        """「⏹ 中止」。"""
        if self._controller is not None:
            self._controller.stop()
            self.progress_panel.append_log("已请求中止，将在记录边界退出", level=LogLevel.WARN.value)

    def _reset_runtime_views(self) -> None:
        """新一轮跑批前清空进度 / 结果视图（保留日志便于追溯）。"""
        self.progress_panel.reset_progress()
        self.summary_panel.clear()
        self.session.clear()
        self.workbench.set_session(self.session)
        self._update_workbench_tab()

    # ─────────────────────── 信号槽（后台 → UI）───────────────────────

    def _on_progress(self, progress: TaskProgress) -> None:
        """进度事件：更新第二区进度条 / 计数 / 日志。"""
        self.progress_panel.apply_progress(progress)
        if progress.counts:
            self.summary_panel.apply_counts(
                progress.counts, processed=progress.processed, total=progress.total
            )

    def _on_state(self, state: ControllerState) -> None:
        """状态变化：驱动第一区按钮文案 + 第三区工作台入口 + 工作台页签可见性。"""
        self._last_state = state
        self.data_source_panel.set_state(state)
        self.summary_panel.set_run_state(state)
        self._update_workbench_tab()
        self.statusBar().showMessage(f"状态：{state.value}", 8000)

    def _update_workbench_tab(self) -> None:
        """按控制器状态 + 是否有结果，控制「人工复核工作台」页签可见性（v0.2.0 点 4）。

        规则：

          * ``FINISHED`` → 显示；
          * ``ABORTED`` / ``FAILED`` **且已产出结果** → 显示（Q4-附：允许中止后进工作台）；
          * ``ABORTED`` / ``FAILED`` **无结果** / 初始 / ``RUNNING`` / ``PAUSED`` → 隐藏。

        「是否有结果」以 :meth:`app.session.AppSession.results`（= 已产出的 CheckResult）
        非空判定，与四卡计数同源。
        """
        state = self._last_state
        has_results = bool(self.session.results())
        if state == ControllerState.FINISHED:
            visible = True
        elif state in (ControllerState.ABORTED, ControllerState.FAILED):
            visible = has_results
        else:
            visible = False
        self.tabs.setTabVisible(_WORKBENCH_TAB_INDEX, visible)

    def _on_breakpoint(self, summary: BreakpointSummary) -> None:
        """断点探测结果：**仅未完成断点显红字**（禁令 2）。"""
        self._last_breakpoint = summary
        if summary.has_unfinished:
            # 红字模板由 BreakpointSummary.hint_text 生成，逐字一致
            self.data_source_panel.show_breakpoint_hint(summary.hint_text())
            self.progress_panel.append_log(summary.hint_text(), level=LogLevel.INFO.value)
        else:
            self.data_source_panel.clear_breakpoint_hint()

    def _on_finished(self, outcome: PipelineOutcome) -> None:
        """跑批结束：刷新第三区 + 复核工作台 + 建 ReviewStore。"""
        self._last_outcome = outcome
        # 结果集回填
        self.session.replace(list(outcome.results), ticket_no=outcome.ticket_no)
        self.summary_panel.apply_counts(
            dict(outcome.counts), processed=outcome.processed, total=outcome.total
        )
        self.summary_panel.show_artifacts(dict(outcome.output_paths))
        self.workbench.set_session(self.session)
        # v0.2.0 点 4：结果落定后刷新工作台页签可见性（与 _on_state 双保险）
        self._update_workbench_tab()

        level = LogLevel.ERROR.value if outcome.failed else LogLevel.INFO.value
        self.progress_panel.append_log(outcome.message, level=level)
        self.statusBar().showMessage(outcome.message, 15000)

        # 暂停 / 失败时保留断点红字提示
        if outcome.paused:
            self.data_source_panel.show_status(f"已自动暂停：{outcome.pause_reason}", error=True)
        elif outcome.aborted:
            self.data_source_panel.show_status("已中止：已完成结果已保存，可『继续执行』续跑。")
        elif outcome.ok:
            self.data_source_panel.clear_breakpoint_hint()
            self.data_source_panel.show_status("跑批完成。")

    # ─────────────────────── 复核工作台 ───────────────────────

    def _ensure_review_store(self) -> ReviewStore | None:
        """按需建 ReviewStore（需要过程产出目录）。"""
        inputs = self._current_inputs()
        process_dir = inputs["process_dir"]
        if not process_dir:
            return None
        if self._review_store is None or str(getattr(self._review_store, "_process_dir", "")) != str(
            Path(process_dir)
        ):
            self._review_store = ReviewStore(self.session, process_dir, allowed_root=process_dir)
        return self._review_store

    def _on_verdict_applied(self, key: str, verdict: str, note: str, mark_missing: bool) -> None:
        """应用一次重判（幂等，写回结果集 + 刷新第三区）。"""
        store = self._ensure_review_store()
        if store is None:
            self.statusBar().showMessage("请先填写过程产出目录后再复核", 8000)
            return
        try:
            store.set_verdict(key, verdict, note, mark_missing=mark_missing)
        except Exception as exc:  # noqa: BLE001 - 复核失败须提示而非闪退
            from infra.errors import user_message_of

            QMessageBox.warning(self, "复核失败", user_message_of(exc))
            return
        # 刷新视图（幂等：以最终一次为准）
        self.summary_panel.apply_counts(
            self.session.counts(),
            processed=len(self.session.results()),
            total=len(self.session.results()),
        )
        self.workbench.reload()
        self.statusBar().showMessage(
            f"已重判：{key} → {verdict}" + ("（已标记待补图）" if mark_missing else ""), 8000
        )

    def _on_export_reviewed(self) -> None:
        """保存本轮留痕 + 覆盖三产物（成果产出）+ 另存复核后 JSON。"""
        store = self._ensure_review_store()
        if store is None:
            QMessageBox.information(self, "无法重导出", "请先填写过程产出目录。")
            return
        if not self.session.results():
            QMessageBox.information(self, "无法重导出", "当前没有校验结果可导出。")
            return

        inputs = self._current_inputs()
        ticket = inputs["ticket_no"] or self.session.ticket_no or "UNKNOWN"
        try:
            from core.result_exporter import ResultExporter

            exporter = ResultExporter(
                inputs["result_dir"],
                inputs["process_dir"],
                allowed_result_root=inputs["result_dir"],
                allowed_process_root=inputs["process_dir"],
            )
            round_path = store.save_round()
            artifacts = exporter.export_all(list(self.session.results()), ticket)
            reviewed_path = store.save_reviewed_json(exporter)
        except Exception as exc:  # noqa: BLE001 - 导出失败须明确提示
            from infra.errors import user_message_of

            QMessageBox.critical(self, "重导出失败", user_message_of(exc))
            return

        self.summary_panel.show_artifacts(
            {
                "summary": str(artifacts["summary"]),
                "review_csv": str(artifacts["review"]),
                "detail_json": str(artifacts["detail"]),
            }
        )
        QMessageBox.information(
            self,
            "重导出完成",
            "已保存本轮留痕并覆盖三产物：\n"
            f"· 留痕：{round_path}\n"
            f"· 复核后 JSON：{reviewed_path}\n"
            f"· 汇总表：{artifacts['summary']}",
        )

    # ─────────────────────── 槽函数 ───────────────────────

    def _on_reload_rules(self) -> None:
        """菜单「重载规则文件」：手动重载规则快照（v1.1 C.3）。"""
        try:
            from core.rule_repository import RuleRepository

            repo = RuleRepository()
            repo.load_all()
            warnings = repo.validate()
            self.log.info("规则已重载，将在下次跑批生效（警告 %d 条）", len(warnings))
            QMessageBox.information(self, "重载规则", "规则已重载，将在下次跑批生效。")
        except Exception as exc:  # noqa: BLE001 - UI 层需兜住一切，避免闪退
            from infra.errors import user_message_of

            self.log.error("规则重载失败：%s", exc)
            QMessageBox.critical(self, "重载规则失败", user_message_of(exc))

    def _on_about(self) -> None:
        """菜单「关于」。"""
        QMessageBox.about(
            self,
            "关于",
            f"<b>{APP_TITLE}</b><br/>版本：v{APP_VERSION}<br/><br/>"
            "对报关申报要素（品牌 / 型号）与产品实拍标签做自动化交叉校验，"
            "输出带完整证据链的校验汇总表与待人工复核清单。<br/><br/>"
            "口径来源：《报关申报要素自动校验 SOP v2.0》",
        )

    # ─────────────────────── 生命周期 ───────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写命名
        """关闭事件：安全退出（检测 Worker 存活 → 询问 → 中止）。

        Args:
            event: Qt 关闭事件。
        """
        controller = self._controller
        if controller is not None and controller.is_running():
            answer = QMessageBox.question(
                self,
                "确认退出",
                "校验任务仍在运行，确定要中止并退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            try:
                controller.stop()
                controller.wait(timeout=10.0)
            except Exception:  # noqa: BLE001 - 退出路径绝不能抛异常
                pass

        self.log.info("主窗口关闭，程序退出")
        event.accept()


def _register_signal_log_sink(window: MainWindow) -> Any:
    """把 :class:`LogBus` 接入 Qt 信号（状态栏提示）。

    Args:
        window: 主窗口实例。

    Returns:
        注册的 sink 回调（供注销）。
    """
    from infra.logger import attach_signal_sink

    def sink(text: str, level: LogLevel) -> None:
        window.statusBar().showMessage(text, 5000)

    attach_signal_sink(sink)
    return sink
