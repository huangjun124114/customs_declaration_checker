"""Qt 跑批工作线程封装（ui.worker）。

把 :class:`app.run_controller.RunControllerCore`（Qt-free 状态机）包装为 Qt 对象：

  * :class:`RunSignals` —— 承载 ``progress`` / ``finished`` / ``state`` / ``breakpoint``
    四类信号（架构设计第 5 节：**所有 UI 更新只通过 ``Signal.emit``**）；
  * :class:`QtRunController` —— ``QObject`` 薄封装，把 ``RunControllerCore`` 的普通回调配
    到 Qt 信号上，并持有一个 ``QThread``（核心控制器在工作线程里跑跑批，信号自动排队到
    UI 线程）。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from app.events import BreakpointSummary, PipelineOutcome, TaskProgress
from app.run_controller import ControllerState, RunControllerCore
from app.session import AppSession

__all__ = ["RunSignals", "QtRunController"]


class RunSignals(QObject):
    """跑批信号集合（跨线程 → UI 线程）。"""

    #: 进度事件（每条 / 每阶段一次）
    progress = Signal(object)
    #: 跑批结束（成功 / 中止 / 暂停 / 失败）
    finished = Signal(object)
    #: 控制器状态变化
    state = Signal(object)
    #: 断点探测结果
    breakpoint = Signal(object)


class QtRunController(QObject):
    """跑批控制器的 Qt 封装（UI 层唯一入口）。

    内部持有 :class:`RunControllerCore`（真状态机 + 后台线程），把它的回调桥接为
    :class:`RunSignals`。UI（``main_window`` / 三区控件）只连信号，绝不直接触碰状态机。

    Args:
        check_task: 跑批编排器（缺省新建；测试可注入 mock）。
        session: 会话结果集（缺省新建）。
        unreachable_k: 连续 K 条不可达自动暂停阈值（禁令 1）。
        parent: Qt 父对象。
    """

    def __init__(
        self,
        check_task: Any = None,
        session: AppSession | None = None,
        *,
        unreachable_k: int = 5,
        parent: QObject | None = None,
    ) -> None:
        """构造 Qt 控制器封装。"""
        super().__init__(parent)
        self.signals = RunSignals(self)
        self.core = RunControllerCore(
            check_task=check_task,
            session=session if session is not None else AppSession(),
            unreachable_k=unreachable_k,
        )
        # 线程对象仅用于表示「工作线程」生命周期；核心内部用 threading.Thread 跑批。
        self._thread = QThread(self)

        # ── 回调 → 信号 桥接 ──
        self.core.progress_cb = self._on_progress
        self.core.finished_cb = self._on_finished
        self.core.state_cb = self._on_state
        self.core.breakpoint_cb = self._on_breakpoint

    # ─────────────────────── 回调桥接 ───────────────────────

    def _on_progress(self, progress: TaskProgress) -> None:
        """核心进度回调 → ``signals.progress``。"""
        self.signals.progress.emit(progress)

    def _on_finished(self, outcome: PipelineOutcome) -> None:
        """核心结束回调 → ``signals.finished``。"""
        self.signals.finished.emit(outcome)

    def _on_state(self, state: ControllerState) -> None:
        """核心状态回调 → ``signals.state``。"""
        self.signals.state.emit(state)

    def _on_breakpoint(self, summary: BreakpointSummary) -> None:
        """核心断点回调 → ``signals.breakpoint``。"""
        self.signals.breakpoint.emit(summary)

    # ─────────────────────── 代理 API（供 UI 调用）───────────────────────

    @property
    def state(self) -> ControllerState:
        """当前控制器状态。"""
        return self.core.state

    def is_running(self) -> bool:
        """是否正在跑批。"""
        return self.core.is_running()

    def probe_breakpoint(self, **kwargs: Any) -> BreakpointSummary:
        """探测断点（代理 :meth:`RunControllerCore.probe_breakpoint`）。"""
        return self.core.probe_breakpoint(**kwargs)

    def start(self, **kwargs: Any) -> bool:
        """「▶ 开始」（代理）。"""
        return self.core.start(**kwargs)

    def restart(self, **kwargs: Any) -> bool:
        """「↻ 重新开始」（代理）。"""
        return self.core.restart(**kwargs)

    def resume_run(self, **kwargs: Any) -> bool:
        """「▶ 继续执行」（代理）。"""
        return self.core.resume_run(**kwargs)

    def pause(self) -> None:
        """「⏸ 暂停」（代理）。"""
        self.core.pause()

    def resume(self) -> None:
        """「▶ 继续」（代理）。"""
        self.core.resume()

    def stop(self) -> None:
        """「⏹ 中止」（代理）。"""
        self.core.stop()

    def wait(self, timeout: float | None = 30.0) -> bool:
        """等待工作线程结束（代理）。"""
        return self.core.wait(timeout=timeout)
