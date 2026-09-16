"""跑批控制器核心（app.run_controller，对应架构设计第 4 节 ``RunController`` +
第 5 节「后台线程模型」+ 12.A / 13.13.4）。

⚠️ **分层架构说明（重要）**：

架构设计同时提出两条相互冲突的约束：

  1. 第 9.6 节 + ``tests/test_architecture_guard.py``：**``app/`` 目录下任何 .py
     禁止 import PySide6 / PyQt**（保证引擎可脱离 GUI 独立运行 / CI）；
  2. 第 4 节类图 + 第 12.A.4 节：``RunController`` **继承 ``QObject`` + ``moveToThread``**，
     并位于 ``app/run_controller.py``。

**本实现的分层裁定**：把 ``RunController`` **一分为二**，两者合起来构成设计中的
"RunController"：

  * **本模块（``app/run_controller.py``）** = :class:`RunControllerCore`
    —— **Qt-free 状态机 + 后台线程编排**：持有 ``CheckTask`` / ``threading.Event`` /
    ``ResumeStore`` / 连续不可达计数（禁令 1）；在**工作线程**里跑 ``CheckTask.run``；
    通过**普通回调**（:class:`RunControllerCore.progress_cb` 等）对外发事件。
  * **``ui/worker.py`` 的 ``QtRunController(QObject)``** = Qt 薄封装：
    ``moveToThread(QThread)`` + ``Signal(object)``，把 :class:`RunControllerCore` 的
    回调转成 Qt 信号（架构设计第 5 节「所有 UI 更新只通过 ``Signal.emit(TaskProgress)``」）。

这样**两条约束同时满足**：引擎层无 Qt 依赖（可 CLI/CI 跑），UI 层拥有 Qt 信号槽封装。

**状态机**（架构设计 12.A.4）::

    IDLE → (probe) → IDLE_WITH_BREAKPOINT | IDLE_NO_BREAKPOINT
         → RUNNING → PAUSED → RUNNING
                   → ABORTED（保留已完成结果 + 写回断点）
         → FINISHED（清空该票断点 + 删除 resume_{票号}.json）

**禁令 1（钉死，13.13.4）**：自动暂停条件**仅**为「连续 K 条 ``CheckResult.unreachable``
== True」；**严禁**改为「连续 K 条 ``NO_IMAGE``」（用户票号填错会误挂整批）。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from enum import Enum

from app.check_task import CheckTask
from app.events import BreakpointSummary, PipelineOutcome, TaskPhase, TaskProgress
from app.session import AppSession
from core.resume_store import ResumeStore, build_resume_path
from infra.logger import Phase, get_logger

__all__ = ["ControllerState", "RunControllerCore", "DEFAULT_UNREACHABLE_K"]


#: 连续 K 条不可达 → 自动暂停（架构设计 13.13.4，禁令 1）
DEFAULT_UNREACHABLE_K: int = 5


class ControllerState(str, Enum):
    """控制器状态机（架构设计 12.A.4）。"""

    IDLE = "IDLE"                                    # 未开始，无断点
    IDLE_NO_BREAKPOINT = "IDLE_NO_BREAKPOINT"        # 未开始，无断点（开始）
    IDLE_WITH_BREAKPOINT = "IDLE_WITH_BREAKPOINT"    # 未开始，有未完成断点（重新开始 + 继续执行）
    RUNNING = "RUNNING"                              # 运行中（暂停 + 中止）
    PAUSED = "PAUSED"                                # 已暂停（继续执行）
    ABORTED = "ABORTED"                              # 已中止
    FINISHED = "FINISHED"                            # 已完成
    FAILED = "FAILED"                                # 致命失败


#: 状态 → 是否「运行中」（按钮状态机用）
_RUNNING_STATES: tuple[ControllerState, ...] = (ControllerState.RUNNING,)


class RunControllerCore:
    """跑批控制器核心（**Qt-free**，架构设计第 4 节 ``RunController`` 的引擎侧）。

    职责：
      * 管理工作线程（``threading.Thread``，非 QThread —— Qt 封装见 ``ui/worker.py``）；
      * 暂停 / 恢复 / 中止（``threading.Event``，在记录边界检查）；
      * 断点探测（``probe_breakpoint``）/ 重新开始（``restart``）/ 继续执行（``resume_run``）；
      * **禁令 1** 的连续不可达计数由 :class:`core.pipeline.CheckPipeline` 内部实现；
        本类负责把「自动暂停」结果暴露给 UI（``paused`` 状态 + 红字文案）。

    事件以**普通回调**形式对外发出（由 ``ui/worker.py`` 转成 Qt 信号）：

      * :attr:`progress_cb`   —— ``Callable[[TaskProgress], None]``
      * :attr:`finished_cb`   —— ``Callable[[PipelineOutcome], None]``
      * :attr:`state_cb`      —— ``Callable[[ControllerState], None]``
      * :attr:`breakpoint_cb` —— ``Callable[[BreakpointSummary], None]``

    Args:
        check_task: 跑批编排器（缺省新建）。
        session: 会话结果集（缺省新建）。
        unreachable_k: 连续 K 条不可达自动暂停阈值（透传给 pipeline）。
    """

    def __init__(
        self,
        check_task: CheckTask | None = None,
        session: AppSession | None = None,
        *,
        unreachable_k: int = DEFAULT_UNREACHABLE_K,
    ) -> None:
        """构造控制器核心。"""
        self._task = check_task if check_task is not None else CheckTask()
        self._session = session if session is not None else AppSession()
        self._unreachable_k = max(1, int(unreachable_k))

        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._state: ControllerState = ControllerState.IDLE
        self._breakpoint: BreakpointSummary = BreakpointSummary()
        self._last_outcome: PipelineOutcome | None = None
        self._log = get_logger(Phase.APP)

        # 事件回调（由 ui/worker.py 注入 Qt 信号发射）
        self.progress_cb: Callable[[TaskProgress], None] | None = None
        self.finished_cb: Callable[[PipelineOutcome], None] | None = None
        self.state_cb: Callable[[ControllerState], None] | None = None
        self.breakpoint_cb: Callable[[BreakpointSummary], None] | None = None

        # 当前跑批参数（start / restart / resume_run 共用）
        self._params: dict[str, object] = {}

    # ══════════════════════════════════════════════════════════
    #  属性
    # ══════════════════════════════════════════════════════════

    @property
    def state(self) -> ControllerState:
        """当前状态。"""
        with self._lock:
            return self._state

    @property
    def phase(self) -> TaskPhase:
        """当前状态映射到 :class:`app.events.TaskPhase`。"""
        return _STATE_PHASE.get(self.state, TaskPhase.IDLE)

    @property
    def session(self) -> AppSession:
        """会话结果集。"""
        return self._session

    @property
    def breakpoint(self) -> BreakpointSummary:
        """最近一次断点摘要。"""
        return self._breakpoint

    @property
    def last_outcome(self) -> PipelineOutcome | None:
        """最近一次跑批结果。"""
        return self._last_outcome

    @property
    def stop_flag(self) -> threading.Event:
        """中止标志。"""
        return self._stop_flag

    @property
    def pause_flag(self) -> threading.Event:
        """暂停标志。"""
        return self._pause_flag

    def is_running(self) -> bool:
        """是否有工作线程在运行。"""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ══════════════════════════════════════════════════════════
    #  断点探测（启动 / 换源时调用）
    # ══════════════════════════════════════════════════════════

    def probe_breakpoint(
        self,
        *,
        ticket_no: str,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        variant: str = "",
        total_count: int = 0,
    ) -> BreakpointSummary:
        """探测断点并驱动按钮文案 / 红字（架构设计 12.A.4 ``probe_breakpoint``）。

        Args:
            ticket_no: 票号。
            excel_path: Excel 路径。
            share_root: 图片根（票号目录层）。
            process_dir: 过程产出目录。
            variant: 结构变体字符串。
            total_count: 预计记录总数。

        Returns:
            :class:`app.events.BreakpointSummary`。
        """
        summary = self._task.probe_breakpoint(
            ticket_no=ticket_no,
            excel_path=excel_path,
            share_root=share_root,
            process_dir=process_dir,
            variant=variant,
            total_count=total_count,
        )
        self._breakpoint = summary

        if not self.is_running():
            self._set_state(
                ControllerState.IDLE_WITH_BREAKPOINT
                if summary.has_unfinished
                else ControllerState.IDLE_NO_BREAKPOINT
            )
        if self.breakpoint_cb is not None:
            _safe_call(self.breakpoint_cb, summary)
        if summary.has_unfinished:
            self._log.info(summary.hint_text())
        return summary

    # ══════════════════════════════════════════════════════════
    #  开始 / 重新开始 / 继续执行
    # ══════════════════════════════════════════════════════════

    def start(
        self,
        *,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        ticket_no: str = "",
        variant: str = "",
        total_count: int = 0,
    ) -> bool:
        """「▶ 开始」：从第 1 条开始跑批（无断点时）。

        Args:
            excel_path: Excel 路径。
            share_root: 图片根。
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
            ticket_no: 票号。
            variant: 结构变体字符串。
            total_count: 记录总数。

        Returns:
            ``True`` 表示已成功启动工作线程。
        """
        return self._launch(
            excel_path=excel_path,
            share_root=share_root,
            result_dir=result_dir,
            process_dir=process_dir,
            ticket_no=ticket_no,
            variant=variant,
            total_count=total_count,
            start_index=0,
            use_resume=False,
        )

    def restart(
        self,
        *,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        ticket_no: str = "",
        variant: str = "",
        total_count: int = 0,
    ) -> bool:
        """「↻ 重新开始」：**清空本票断点**后从第 1 条重跑（12.A.1）。

        Args:
            excel_path: Excel 路径。
            share_root: 图片根。
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
            ticket_no: 票号。
            variant: 结构变体字符串。
            total_count: 记录总数。

        Returns:
            ``True`` 表示已成功启动。
        """
        self._reset_breakpoint(
            ticket_no=ticket_no,
            excel_path=excel_path,
            share_root=share_root,
            process_dir=process_dir,
            variant=variant,
            total_count=total_count,
        )
        return self._launch(
            excel_path=excel_path,
            share_root=share_root,
            result_dir=result_dir,
            process_dir=process_dir,
            ticket_no=ticket_no,
            variant=variant,
            total_count=total_count,
            start_index=0,
            use_resume=False,
        )

    def resume_run(
        self,
        *,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        ticket_no: str = "",
        variant: str = "",
        total_count: int = 0,
    ) -> bool:
        """「▶ 继续执行」：读断点，从 ``done_count`` 之后续跑（已完成的不重跑）。

        Args:
            excel_path: Excel 路径。
            share_root: 图片根。
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
            ticket_no: 票号。
            variant: 结构变体字符串。
            total_count: 记录总数。

        Returns:
            ``True`` 表示已成功启动。
        """
        return self._launch(
            excel_path=excel_path,
            share_root=share_root,
            result_dir=result_dir,
            process_dir=process_dir,
            ticket_no=ticket_no,
            variant=variant,
            total_count=total_count,
            start_index=0,
            use_resume=True,
        )

    # ══════════════════════════════════════════════════════════
    #  暂停 / 恢复 / 中止
    # ══════════════════════════════════════════════════════════

    def pause(self) -> None:
        """「⏸ 暂停」：设置 ``pause_flag``，Worker 在**记录边界**挂起。"""
        if not self.is_running():
            return
        self._pause_flag.set()
        self._set_state(ControllerState.PAUSED)
        self._log.info("已发出暂停请求，将在当前记录边界挂起")

    def resume(self) -> None:
        """「继续」：清除 ``pause_flag``，Worker 从挂起点继续。"""
        if self._pause_flag.is_set():
            self._pause_flag.clear()
            if self.is_running():
                self._set_state(ControllerState.RUNNING)
            self._log.info("已恢复执行")

    def stop(self) -> None:
        """「⏹ 中止」：设置 ``stop_flag``，Worker 在记录边界退出并 flush 断点。"""
        self._stop_flag.set()
        self._pause_flag.clear()  # 解除暂停，让 Worker 能走到边界检查中止
        self._log.warning("已发出中止请求，已完成记录将落盘保留")

    # ══════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════

    def wait(self, timeout: float | None = None) -> bool:
        """等待工作线程结束（供 ``closeEvent`` 安全退出）。

        Args:
            timeout: 超时秒数；``None`` 表示无限等待。

        Returns:
            ``True`` 表示线程已结束。
        """
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def thread_alive(self) -> bool:
        """工作线程是否存活（``MainWindow.closeEvent`` 用）。"""
        return self.is_running()

    # ══════════════════════════════════════════════════════════
    #  内部：启动工作线程
    # ══════════════════════════════════════════════════════════

    def _launch(self, *, start_index: int, use_resume: bool, **params: object) -> bool:
        """启动工作线程（统一入口）。

        Args:
            start_index: 起始下标。
            use_resume: 是否使用断点续跑。
            **params: 跑批参数（excel_path / share_root / ...）。

        Returns:
            ``True`` 表示已成功启动。
        """
        if self.is_running():
            self._log.warning("上一次跑批仍在运行，忽略本次启动请求")
            return False

        self._stop_flag.clear()
        self._pause_flag.clear()
        self._params = dict(params)
        self._last_outcome = None

        # 续跑：读断点填 start_index / already_done
        already_done: set[str] = set()
        resume_store: ResumeStore | None = None
        if use_resume:
            summary = self._breakpoint
            if summary.matched:
                already_done = set(summary.done_keys)
                start_index = 0  # 由 pipeline 依据 resume_store.done_keys 跳过
            else:
                # 重新探测一次（用户可能刚恢复共享盘）
                summary = self.probe_breakpoint(
                    ticket_no=str(params.get("ticket_no", "") or ""),
                    excel_path=params.get("excel_path", ""),  # type: ignore[arg-type]
                    share_root=params.get("share_root", ""),  # type: ignore[arg-type]
                    process_dir=params.get("process_dir", ""),  # type: ignore[arg-type]
                    variant=str(params.get("variant", "") or ""),
                    total_count=int(params.get("total_count", 0) or 0),
                )
                already_done = set(summary.done_keys)
            if already_done:
                resume_store = self._make_resume_store(**params)  # type: ignore[arg-type]
                if resume_store is not None:
                    snapshot = resume_store.load_if_match()
                    if snapshot is not None:
                        already_done = set(snapshot.done_keys)

        self._set_state(ControllerState.RUNNING)

        def _worker() -> None:
            try:
                outcome = self._task.run(
                    excel_path=params.get("excel_path", ""),  # type: ignore[arg-type]
                    share_root=params.get("share_root", ""),  # type: ignore[arg-type]
                    result_dir=params.get("result_dir", ""),  # type: ignore[arg-type]
                    process_dir=params.get("process_dir", ""),  # type: ignore[arg-type]
                    ticket_no=str(params.get("ticket_no", "") or ""),
                    start_index=start_index,
                    resume_store=resume_store,
                    progress_cb=self._emit_progress,
                    stop_flag=self._stop_flag,
                    pause_flag=self._pause_flag,
                    already_done=already_done,
                )
            except Exception as exc:  # noqa: BLE001 - Worker 顶层兜底，避免线程静默崩溃
                self._log.error(f"工作线程未预期异常：{type(exc).__name__}: {exc}")
                outcome = PipelineOutcome(
                    ok=False,
                    failed=True,
                    message=f"跑批未预期异常：{type(exc).__name__}",
                )
            self._on_finished(outcome)

        thread = threading.Thread(target=_worker, name="customs-run", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        self._log.info("跑批工作线程已启动")
        return True

    def _make_resume_store(self, **params: object) -> ResumeStore | None:
        """按参数创建绑定当前数据源的断点存储。"""
        try:
            return self._task.create_resume_store(
                ticket_no=str(params.get("ticket_no", "") or ""),
                excel_path=params.get("excel_path", ""),  # type: ignore[arg-type]
                share_root=params.get("share_root", ""),  # type: ignore[arg-type]
                process_dir=params.get("process_dir", ""),  # type: ignore[arg-type]
                variant=str(params.get("variant", "") or ""),
                total_count=int(params.get("total_count", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001 - 断点创建失败不应阻止续跑
            self._log.warning(f"断点存储创建失败（将从头跑）：{exc}")
            return None

    def _reset_breakpoint(self, **params: object) -> None:
        """清空本票断点（「重新开始」时调用，12.A.1）。"""
        try:
            store = self._task.create_resume_store(
                ticket_no=str(params.get("ticket_no", "") or ""),
                excel_path=params.get("excel_path", ""),  # type: ignore[arg-type]
                share_root=params.get("share_root", ""),  # type: ignore[arg-type]
                process_dir=params.get("process_dir", ""),  # type: ignore[arg-type]
                variant=str(params.get("variant", "") or ""),
                total_count=int(params.get("total_count", 0) or 0),
            )
            store.reset()
        except Exception as exc:  # noqa: BLE001 - 重置失败不应阻断重跑
            self._log.warning(f"断点重置失败（将从第 1 条重跑）：{exc}")
        self._breakpoint = BreakpointSummary(
            matched=False,
            ticket_no=str(params.get("ticket_no", "") or ""),
            total_count=int(params.get("total_count", 0) or 0),
        )

    def _emit_progress(self, progress: TaskProgress) -> None:
        """接收 pipeline 进度事件并转发（同时同步 session 计数）。"""
        if progress.phase == TaskPhase.FINISHED:
            pass
        if self.progress_cb is not None:
            _safe_call(self.progress_cb, progress)

    def _on_finished(self, outcome: PipelineOutcome) -> None:
        """工作线程结束回调：更新会话 / 状态 / 导出 / 断点清理。"""
        self._last_outcome = outcome
        self._session.replace(outcome.results, ticket_no=outcome.ticket_no)
        self._session.set_ticket_no(outcome.ticket_no)

        if outcome.failed:
            self._set_state(ControllerState.FAILED)
        elif outcome.paused:
            self._set_state(ControllerState.PAUSED)
            self._log.warning(outcome.pause_reason or "共享盘断连，已自动暂停")
        elif outcome.aborted:
            self._set_state(ControllerState.ABORTED)
        else:
            self._set_state(ControllerState.FINISHED)
            self._clear_breakpoint_on_success(outcome)

        if self.finished_cb is not None:
            _safe_call(self.finished_cb, outcome)
        self._log.info(outcome.message)

    def _clear_breakpoint_on_success(self, outcome: PipelineOutcome) -> None:
        """跑批**完成**清空该票断点并删除 ``resume_{票号}.json``（12.A.4 FINISHED）。"""
        params = self._params
        try:
            store = self._task.create_resume_store(
                ticket_no=outcome.ticket_no or str(params.get("ticket_no", "") or ""),
                excel_path=params.get("excel_path", ""),  # type: ignore[arg-type]
                share_root=params.get("share_root", ""),  # type: ignore[arg-type]
                process_dir=params.get("process_dir", ""),  # type: ignore[arg-type]
                variant=str(params.get("variant", "") or ""),
                total_count=int(params.get("total_count", 0) or 0),
            )
            # 仅当结果集完整（全部记录完成）时才清断点
            if outcome.total and outcome.processed >= outcome.total:
                path = build_resume_path(str(params.get("process_dir", "") or ""), outcome.ticket_no)
                store.reset()
                self._log.info(f"跑批完成，已清空断点：{path.name}")
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响结果
            self._log.debug(f"断点清理跳过：{exc}")
        self._breakpoint = BreakpointSummary(
            matched=False,
            ticket_no=outcome.ticket_no,
            total_count=outcome.total,
        )
        if self.breakpoint_cb is not None:
            _safe_call(self.breakpoint_cb, self._breakpoint)

    def _set_state(self, state: ControllerState) -> None:
        """更新状态并通知 UI。"""
        with self._lock:
            if self._state == state:
                return
            self._state = state
        if self.state_cb is not None:
            _safe_call(self.state_cb, state)


#: 状态 → TaskPhase 映射
_STATE_PHASE: dict[ControllerState, TaskPhase] = {
    ControllerState.IDLE: TaskPhase.IDLE,
    ControllerState.IDLE_NO_BREAKPOINT: TaskPhase.IDLE,
    ControllerState.IDLE_WITH_BREAKPOINT: TaskPhase.IDLE,
    ControllerState.RUNNING: TaskPhase.OCR,
    ControllerState.PAUSED: TaskPhase.PAUSED,
    ControllerState.ABORTED: TaskPhase.ABORTED,
    ControllerState.FINISHED: TaskPhase.FINISHED,
    ControllerState.FAILED: TaskPhase.FAILED,
}


def _safe_call(cb: Callable[..., None], *args: object) -> None:
    """安全调用回调（异常被吞，不得影响跑批主流程）。"""
    try:
        cb(*args)
    except Exception:  # noqa: BLE001 - 回调故障不得中断主流程
        pass


def is_running_state(state: ControllerState) -> bool:
    """判断状态是否属于「运行中」（供 UI 按钮状态机复用）。"""
    return state in _RUNNING_STATES


def check_no_image_pause_forbidden() -> str:
    """返回禁令 1 的说明文案（供测试 / 文档引用，确保禁令不被误改）。

    Returns:
        禁令说明字符串。
    """
    return (
        "自动暂停触发条件仅允许「连续 K 条 CheckResult.unreachable == True」；"
        "严禁使用「连续 K 条 Verdict.NO_IMAGE」（用户票号填错会误挂整批）。"
    )
