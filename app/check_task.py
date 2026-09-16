"""一次跑批编排（app.check_task，对应架构设计第 4 节 ``CheckTask`` + 第 5 节链路 A）。

:class:`CheckTask` 是 **GUI 与引擎之间的唯一编排入口**：

  * 组合 :class:`app.path_policy.PathPolicy` + :class:`app.preflight.PreflightRunner`
    + :class:`core.pipeline.CheckPipeline`；
  * 把 ``core`` 的 :class:`core.pipeline.PipelineProgress` 转成 ``app`` 的
    :class:`app.events.TaskProgress`（供 ``RunController`` 的 Qt 信号携带）；
  * 管理断点（``ResumeStore``）的**创建 / 续跑 / 重开 / 清空**；
  * 提供 :meth:`dry_run`（预演）供 UI 展示命中率（Phase2 干跑，不跑 OCR）。

**无全局状态**：:meth:`run` 可被外层循环多次调用（Q2 多票扩展预留）。

**分层约束**：``app/``（L3）只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from app.events import PipelineOutcome, TaskPhase, TaskProgress
from app.path_policy import PathPolicy, PreflightReport
from app.preflight import BreakpointProbe, PreflightRunner
from core.constants import ALL_VERDICTS
from core.image_resolver import ImageResolver
from core.models import Fingerprint
from core.pipeline import CheckPipeline, PipelineProgress
from core.resume_store import ResumeStore, build_resume_path
from core.rule_repository import RuleRepository
from infra.errors import CustomsCheckerError, user_message_of
from infra.logger import LogLevel, Phase, get_logger

__all__ = ["CheckTask", "DryRunReport"]


#: ``core.pipeline`` 阶段字符串 → :class:`app.events.TaskPhase`
_PHASE_MAP: dict[str, TaskPhase] = {
    "idle": TaskPhase.IDLE,
    "probe": TaskPhase.PROBE,
    "resolve": TaskPhase.RESOLVE,
    "ocr": TaskPhase.OCR,
    "export": TaskPhase.EXPORT,
    "finished": TaskPhase.FINISHED,
    "paused": TaskPhase.PAUSED,
    "aborted": TaskPhase.ABORTED,
    "failed": TaskPhase.FAILED,
}


@dataclass
class DryRunReport:
    """干跑预演报告（SOP Phase2：展示命中率，不跑 OCR）。

    Attributes:
        ok: 是否成功得到预演结果。
        ticket_no: 票号。
        variant: 结构变体字符串。
        record_count: 记录条数。
        hit_count: 命中图片的记录数。
        hit_rate: 命中率（0.0–1.0）。
        unreachable_count: 共享盘不可达的记录数（预演阶段）。
        not_found_count: 目录不存在（缺图）的记录数。
        message: 面向用户的提示（命中率异常低时给出警告，R6）。
        notes: 探查说明。
    """

    ok: bool = False
    ticket_no: str = ""
    variant: str = ""
    record_count: int = 0
    hit_count: int = 0
    hit_rate: float = 0.0
    unreachable_count: int = 0
    not_found_count: int = 0
    message: str = ""
    notes: list[str] = field(default_factory=list)


class CheckTask:
    """一次跑批编排器（架构设计第 4 节 ``CheckTask``）。

    Args:
        rule_repository: 规则仓库（缺省新建并 ``load_all``）。
        path_policy: 路径策略（缺省新建）。
        pipeline: 引擎编排器（缺省按配置新建）。
    """

    #: 命中率低于该值时给出「疑似结构变体未适配」警告（R6）
    LOW_HIT_RATE_THRESHOLD: float = 0.30

    def __init__(
        self,
        rule_repository: RuleRepository | None = None,
        *,
        path_policy: PathPolicy | None = None,
        pipeline: CheckPipeline | None = None,
    ) -> None:
        """构造跑批编排器。"""
        self._repo = rule_repository if rule_repository is not None else RuleRepository()
        if rule_repository is None:
            self._repo.load_all()
        self._policy = path_policy if path_policy is not None else PathPolicy()
        self._pipeline = pipeline
        self._preflight = PreflightRunner(self._repo, self._policy)
        self._probe_helper = BreakpointProbe("", self._repo)
        self._log = get_logger(Phase.PHASE0)

    # ══════════════════════════════════════════════════════════
    #  预检 / 断点
    # ══════════════════════════════════════════════════════════

    def preflight(
        self,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        *,
        ticket_no: str = "",
    ) -> PreflightReport:
        """执行预检（委托 :class:`app.preflight.PreflightRunner`）。"""
        return self._preflight.run(excel_path, share_root, ticket_no=ticket_no)

    def probe_breakpoint(
        self,
        *,
        ticket_no: str,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        variant: str = "",
        total_count: int = 0,
    ):
        """探测断点（委托 :class:`app.preflight.BreakpointProbe`）。

        Returns:
            :class:`app.events.BreakpointSummary`。
        """
        probe = BreakpointProbe(process_dir, self._repo)
        fingerprint = probe.build_fingerprint(
            ticket_no=ticket_no,
            excel_path=excel_path,
            share_root=share_root,
            variant=variant,
            total_count=total_count,
        )
        return probe.probe(fingerprint, ticket_no)

    def build_fingerprint(
        self,
        *,
        ticket_no: str,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        variant: str,
        total_count: int = 0,
    ) -> Fingerprint:
        """构造当前数据源指纹（供 ``RunController`` 创建 ``ResumeStore``）。"""
        return BreakpointProbe("", self._repo).build_fingerprint(
            ticket_no=ticket_no,
            excel_path=excel_path,
            share_root=share_root,
            variant=variant,
            total_count=total_count,
        )

    # ══════════════════════════════════════════════════════════
    #  干跑预演（Phase2，不跑 OCR）
    # ══════════════════════════════════════════════════════════

    def dry_run(
        self,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        *,
        ticket_no: str = "",
    ) -> DryRunReport:
        """干跑预演：探查 + 图片匹配，**不跑 OCR**（SOP Phase2）。

        用于在真正跑批前展示命中率；命中率异常低时给出「疑似结构变体未适配」警告
        （R6 / README 6.2）。

        Args:
            excel_path: Excel 路径。
            share_root: 图片根（票号目录层）。
            ticket_no: 票号（用户手填优先）。

        Returns:
            :class:`DryRunReport`。
        """
        report = DryRunReport()
        try:
            probe_result = self._preflight.probe_only(excel_path)
        except CustomsCheckerError as exc:
            report.message = exc.user_message
            return report
        except Exception as exc:  # noqa: BLE001 - 预演须兜住一切
            report.message = user_message_of(exc)
            return report

        report.ticket_no = (ticket_no or "").strip() or (probe_result.ticket_no or "").strip()
        report.variant = probe_result.variant.value
        report.record_count = len(probe_result.records)
        report.notes = list(probe_result.notes)

        resolver = ImageResolver()
        share = self._policy.clean_dialog_path(share_root)
        for record in probe_result.records:
            try:
                evidences = resolver.resolve(record, share, ticket_no=report.ticket_no)
            except Exception:  # noqa: BLE001
                evidences = []
            record.evidences = evidences
            if evidences:
                report.hit_count += 1
            if any(ev.unreachable for ev in evidences):
                report.unreachable_count += 1
            if evidences and all(ev.not_found for ev in evidences):
                report.not_found_count += 1

        if report.record_count > 0:
            report.hit_rate = report.hit_count / float(report.record_count)
        report.ok = True

        if report.unreachable_count:
            report.message = (
                f"共享盘不可达：{report.unreachable_count} 条记录读图失败，"
                "请检查共享盘连接与权限。"
            )
        elif report.hit_rate < self.LOW_HIT_RATE_THRESHOLD and report.record_count > 0:
            report.message = (
                f"图片命中率偏低（{report.hit_rate:.0%}），疑似结构变体未适配，"
                "建议核查 Excel 列与图片目录结构。"
            )
        else:
            report.message = (
                f"干跑预演完成：命中 {report.hit_count}/{report.record_count} 条"
                f"（命中率 {report.hit_rate:.0%}）"
            )
        self._log.info(report.message)
        return report

    # ══════════════════════════════════════════════════════════
    #  跑批
    # ══════════════════════════════════════════════════════════

    def run(  # noqa: C901 - 编排入口，长是合理的
        self,
        *,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        ticket_no: str = "",
        start_index: int = 0,
        resume_store: ResumeStore | None = None,
        progress_cb: object | None = None,
        stop_flag: threading.Event | None = None,
        pause_flag: threading.Event | None = None,
        already_done: set[str] | None = None,
        export: bool = True,
    ) -> PipelineOutcome:
        """执行一次跑批（把 ``core.pipeline`` 事件转为 :class:`app.events.TaskProgress`）。

        Args:
            excel_path: Excel 路径。
            share_root: 图片根（票号目录层）。
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
            ticket_no: 票号。
            start_index: 续跑起始下标。
            resume_store: 断点存储（缺省按需新建）。
            progress_cb: ``Callable[[TaskProgress], None]`` 进度回调。
            stop_flag: 中止标志。
            pause_flag: 暂停标志。
            already_done: 已完成的 key 集合（续跑跳过）。
            export: 是否导出三产物。

        Returns:
            :class:`app.events.PipelineOutcome`。
        """
        emit = self._make_task_emitter(progress_cb)

        excel = self._policy.clean_dialog_path(excel_path)
        share = self._policy.clean_dialog_path(share_root)
        result_path, process_path = self._policy.resolve_outputs(
            result_dir=result_dir, process_dir=process_dir
        )

        emit(TaskProgress.make(TaskPhase.PREFLIGHT, message="数据源预检…"))
        preflight = self.preflight(excel, share, ticket_no=ticket_no)
        if not preflight.ok:
            message = preflight.errors[0] if preflight.errors else "预检未通过"
            emit(TaskProgress.make(TaskPhase.FAILED, message=message, level=LogLevel.ERROR))
            return PipelineOutcome(
                ok=False, failed=True, message=message, ticket_no=preflight.ticket_hint
            )

        resolved_ticket = (ticket_no or "").strip() or preflight.ticket_hint

        pipeline = self._pipeline or self._build_pipeline(stop_flag, pause_flag)
        # 注入式 pipeline（如测试 mock）在构造时已绑定自己的 flag；若调用方本次
        # 显式传入 stop_flag / pause_flag，则重绑，确保轮询能实时拿到中止 / 暂停信号。
        # 否则「注入 pipeline + run 时给 flag」的组合会退化成 flag 被忽略（历史上导致
        # 中止命令不生效）。
        if self._pipeline is not None:
            self._rebind_flags(pipeline, stop_flag, pause_flag)

        def _on_pipeline(progress: PipelineProgress) -> None:
            emit(self._to_task_progress(progress))

        try:
            result = pipeline.run(
                excel_path=excel,
                share_root=share,
                result_dir=result_path,
                process_dir=process_path,
                ticket_no=resolved_ticket,
                start_index=start_index,
                resume_store=resume_store,
                progress_cb=_on_pipeline,
                export=export,
                allowed_result_root=result_path,
                allowed_process_root=process_path,
            )
        except CustomsCheckerError as exc:
            emit(TaskProgress.make(TaskPhase.FAILED, message=exc.user_message, level=LogLevel.ERROR))
            return PipelineOutcome(
                ok=False, failed=True, message=exc.user_message, ticket_no=resolved_ticket
            )
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免 Worker 崩溃无提示
            message = user_message_of(exc)
            self._log.error(f"跑批未预期异常：{type(exc).__name__}: {exc}")
            emit(TaskProgress.make(TaskPhase.FAILED, message=message, level=LogLevel.ERROR))
            return PipelineOutcome(
                ok=False, failed=True, message=message, ticket_no=resolved_ticket
            )

        return PipelineOutcome(
            ok=result.ok,
            aborted=result.aborted,
            failed=result.failed,
            paused=result.paused,
            results=list(result.results),
            counts=dict(result.counts),
            output_paths=dict(result.output_paths),
            ticket_no=result.ticket_no,
            total=result.total,
            processed=result.processed,
            message=result.message,
            pause_key=result.pause_key,
            pause_reason=result.pause_reason,
            notes=list(result.notes),
        )

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════

    def _build_pipeline(
        self,
        stop_flag: threading.Event | None,
        pause_flag: threading.Event | None,
    ) -> CheckPipeline:
        """按当前规则仓库构造引擎编排器。"""
        return CheckPipeline(
            self._repo,
            stop_flag=stop_flag,
            pause_flag=pause_flag,
        )

    @staticmethod
    def _rebind_flags(
        pipeline: CheckPipeline,
        stop_flag: threading.Event | None,
        pause_flag: threading.Event | None,
    ) -> None:
        """把调用方本次传入的 ``stop_flag`` / ``pause_flag`` 重绑到注入式 pipeline。

        ``core.pipeline.CheckPipeline`` 只在构造时绑定 flag 且未暴露 setter（**不可修改
        T04 引擎文件**），因此此处刻意访问其私有属性完成重绑。若调用方传 ``None`` 则保持
        原绑定不变。这样「注入 pipeline + run 时给 flag」的中止 / 暂停命令才能实时生效。

        Args:
            pipeline: 注入的引擎编排器。
            stop_flag: 本次中止标志；``None`` 表示不改。
            pause_flag: 本次暂停标志；``None`` 表示不改。
        """
        if stop_flag is not None:
            pipeline._stop_flag = stop_flag  # noqa: SLF001 - core 未暴露 setter（不可改引擎）
        if pause_flag is not None:
            pipeline._pause_flag = pause_flag  # noqa: SLF001 - core 未暴露 setter（不可改引擎）

    def _make_task_emitter(self, cb: object | None):
        """把 ``Callable[[TaskProgress], None]`` 包装为安全发射器。"""

        def emit(progress: TaskProgress) -> None:
            if cb is None:
                return
            try:
                cb(progress)  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001 - 回调故障不得中断跑批
                self._log.debug(f"进度回调异常（已忽略）：{exc}")

        return emit

    @staticmethod
    def _to_task_progress(progress: PipelineProgress) -> TaskProgress:
        """把 ``core`` 的进度事件转为 ``app`` 的 :class:`app.events.TaskProgress`。"""
        phase = _PHASE_MAP.get(progress.phase, TaskPhase.OCR)
        counts = {v.value: 0 for v in ALL_VERDICTS}
        for key, value in (progress.counts or {}).items():
            counts[key] = int(value)
        try:
            level = LogLevel(str(progress.level).upper())
        except ValueError:
            level = LogLevel.INFO
        return TaskProgress.make(
            phase,
            current=progress.current,
            total=progress.total,
            current_key=progress.current_key,
            counts=counts,
            message=progress.message,
            level=level,
        )

    def create_resume_store(
        self,
        *,
        ticket_no: str,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        variant: str = "",
        total_count: int = 0,
    ) -> ResumeStore:
        """创建一个绑定当前数据源的 :class:`core.resume_store.ResumeStore`。

        Args:
            ticket_no: 票号。
            excel_path: Excel 路径。
            share_root: 图片根。
            process_dir: 过程产出目录。
            variant: 结构变体字符串。
            total_count: 记录总数。

        Returns:
            新的断点存储实例。
        """
        fingerprint = self.build_fingerprint(
            ticket_no=ticket_no,
            excel_path=excel_path,
            share_root=share_root,
            variant=variant,
            total_count=total_count,
        )
        path = build_resume_path(process_dir, ticket_no)
        return ResumeStore(path, fingerprint, allowed_root=process_dir, autosave=True)
