"""引擎编排（core.pipeline，对应架构设计第 3 节 + 第 5 节链路 A）。

:class:`CheckPipeline` 把引擎串起来（**纯 Python，无 Qt 依赖**，可 CLI / pytest 直跑）::

    ExcelProbe.probe → ImageResolver.resolve（干跑）
      → 分批 OcrEngine.recognize_batch → JudgeEngine.judge
      → ResumeStore 逐条落盘 → ResultExporter 三产物导出

**关键契约**：

  * **分批**：默认 6 条/批；单条图片数 > ``heavy_image_threshold`` 自动降为 3 条/批
    （架构设计 12 / R3）。
  * **异常隔离**：单条记录任何异常被捕获 → 记 ERROR + 该条退回重跑（下次续跑补齐）
    → **继续下一批**，绝不中断整批（架构设计第 5 节「异常隔离」）。
  * **暂停 / 中止**：由 ``threading.Event`` 实现，在**记录边界**检查（架构设计第 5 节）。
    中止后 ``ResumeStore.flush()`` 落盘，**已完成记录绝不丢失**。
  * **自动暂停（禁令 1）**：仅当「连续 K 条含 ``CheckResult.unreachable == True``」时
    触发；**严禁**用「连续 K 条 ``NO_IMAGE``」（用户票号填错会误挂整批，架构设计 13.13.4）。
  * **只读红线**：Excel 以 ``read_only=True`` 打开；图片只读字节；跑批结束写入前
    断言产物归属（越界抛 :class:`infra.errors.OutputPathViolation`）。

**分层约束**：``core/``（L2）只能 import ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.constants import ALL_VERDICTS
from core.element_parser import ElementParser
from core.excel_probe import ExcelProbe
from core.image_resolver import ImageResolver
from core.judge_engine import JudgeEngine
from core.models import CheckResult, DeclarationRecord, ExcelProbeResult, Fingerprint, Verdict
from core.ocr_engine import OcrEngine
from core.result_exporter import ResultExporter
from core.resume_store import ResumeStore, build_resume_path, compute_excel_hash
from core.rule_repository import RuleRepository
from infra.encoding import normalize_path
from infra.errors import CustomsCheckerError
from infra.logger import Phase, get_logger

__all__ = [
    "DEFAULT_UNREACHABLE_K",
    "PipelineProgress",
    "PipelineResult",
    "CheckPipeline",
]


#: 连续 K 条 ``unreachable`` → 自动暂停（架构设计 13.13.4）
DEFAULT_UNREACHABLE_K: int = 5


@dataclass
class PipelineProgress:
    """编排层进度事件（**纯 ``dataclass``**，由 ``app`` 层转为 ``TaskProgress``）。

    Attributes:
        phase: 阶段字符串（``"probe"`` / ``"resolve"`` / ``"ocr"`` / ``"export"`` 等）。
        current: 已处理条数。
        total: 总条数。
        current_key: 当前记录 key。
        counts: 四类判定计数。
        message: 进度消息。
        level: 日志级别字符串（``"INFO"`` / ``"WARN"`` / ``"ERROR"``）。
    """

    phase: str = "idle"
    current: int = 0
    total: int = 0
    current_key: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    message: str = ""
    level: str = "INFO"


@dataclass
class PipelineResult:
    """编排层最终结果（架构设计第 4 节 ``PipelineResult``）。

    Attributes:
        ok: 是否正常完成（未中止 / 未致命失败 / 未暂停）。
        aborted: 是否被用户中止。
        failed: 是否致命失败。
        paused: 是否因共享盘断连自动暂停。
        results: 全部已完成结果（按 key 升序）。
        counts: 四类判定计数。
        output_paths: 三产物落盘路径 ``{"summary"/"review"/"detail": str}``。
        ticket_no: 票号。
        total: 计划处理记录总数。
        processed: 实际完成条数。
        message: 面向用户的总结文案。
        pause_key: 自动暂停时的记录 key。
        pause_reason: 自动暂停原因。
        notes: 附加说明（变体降级提示等）。
        excel_hash: Excel 内容 sha256（指纹）。
    """

    ok: bool = True
    aborted: bool = False
    failed: bool = False
    paused: bool = False
    results: list[CheckResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    ticket_no: str = ""
    total: int = 0
    processed: int = 0
    message: str = ""
    pause_key: str = ""
    pause_reason: str = ""
    notes: list[str] = field(default_factory=list)
    excel_hash: str = ""

    def count_sum(self) -> int:
        """四类判定计数之和。"""
        return sum(int(v) for v in self.counts.values())


#: 进度回调签名
ProgressCallback = Callable[[PipelineProgress], None]


class CheckPipeline:
    """引擎编排器（架构设计第 4 节 ``CheckPipeline``）。

    Args:
        rule_repository: 规则仓库（缺省新建并 ``load_all``）。
        probe: 结构探查器（缺省用 ``rule_repository`` 构造）。
        resolver: 图片解析器（缺省 :class:`core.image_resolver.ImageResolver`）。
        ocr_engine: OCR 编排器（缺省惰性构造；可注入 mock 后端）。
        judge_engine: 判定引擎（缺省用 ``rule_repository`` 构造）。
        exporter: 三产物导出器（缺省 ``None``，在 :meth:`run` 时按目录构造）。
        batch_size: 常规批大小（缺省 6）。
        batch_size_heavy: 图片多时批大小（缺省 3）。
        heavy_image_threshold: 单条图片数超过该值降批（缺省 8）。
        unreachable_k: 连续 K 条不可达自动暂停阈值（缺省 5）。
        stop_flag: 中止标志（``threading.Event``）；``None`` 时新建。
        pause_flag: 暂停标志（``threading.Event``）；``None`` 时新建。
        stop_event_check_interval: 暂停时的轮询间隔（秒），避免忙等。
    """

    def __init__(
        self,
        rule_repository: RuleRepository | None = None,
        *,
        probe: ExcelProbe | None = None,
        resolver: ImageResolver | None = None,
        ocr_engine: OcrEngine | None = None,
        judge_engine: JudgeEngine | None = None,
        exporter: ResultExporter | None = None,
        batch_size: int = 6,
        batch_size_heavy: int = 3,
        heavy_image_threshold: int = 8,
        unreachable_k: int = DEFAULT_UNREACHABLE_K,
        stop_flag: threading.Event | None = None,
        pause_flag: threading.Event | None = None,
        stop_event_check_interval: float = 0.2,
    ) -> None:
        """构造编排器。"""
        self._repo = rule_repository if rule_repository is not None else RuleRepository()
        if rule_repository is None:
            self._repo.load_all()

        self._probe = probe if probe is not None else ExcelProbe(
            parser=ElementParser(self._repo)
        )
        self._resolver = resolver if resolver is not None else ImageResolver()
        self._ocr = ocr_engine if ocr_engine is not None else OcrEngine()
        self._judge = judge_engine if judge_engine is not None else JudgeEngine(self._repo)
        self._exporter = exporter

        self._batch_size = max(1, int(batch_size))
        self._batch_size_heavy = max(1, int(batch_size_heavy))
        self._heavy_threshold = max(1, int(heavy_image_threshold))
        self._unreachable_k = max(1, int(unreachable_k))

        self._stop_flag = stop_flag if stop_flag is not None else threading.Event()
        self._pause_flag = pause_flag if pause_flag is not None else threading.Event()
        self._poll_interval = max(0.01, float(stop_event_check_interval))

        self._log = get_logger(Phase.PHASE3)

    # ══════════════════════════════════════════════════════════
    #  属性（供 RunController 接线）
    # ══════════════════════════════════════════════════════════

    @property
    def stop_flag(self) -> threading.Event:
        """中止标志。"""
        return self._stop_flag

    @property
    def pause_flag(self) -> threading.Event:
        """暂停标志。"""
        return self._pause_flag

    @property
    def rule_repository(self) -> RuleRepository:
        """规则仓库。"""
        return self._repo

    # ══════════════════════════════════════════════════════════
    #  主入口
    # ══════════════════════════════════════════════════════════

    def run(  # noqa: C901 - 编排主流程，长是合理的
        self,
        *,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        ticket_no: str = "",
        start_index: int = 0,
        resume_store: ResumeStore | None = None,
        progress_cb: ProgressCallback | None = None,
        export: bool = True,
        allowed_result_root: str | os.PathLike[str] | None = None,
        allowed_process_root: str | os.PathLike[str] | None = None,
    ) -> PipelineResult:
        """执行一次完整跑批（预检 → 探查 → 解析 → 匹配 → 分批 OCR → 判定 → 导出）。

        Args:
            excel_path: 申报要素 Excel 路径。
            share_root: 图片根（票号目录层，已由 ``PathPolicy`` 解析）。
            result_dir: 成果产出目录。
            process_dir: 过程产出目录。
            ticket_no: 票号（用户手填优先；为空时用探查结果）。
            start_index: 续跑起始下标（＜该下标的记录跳过，不重复 OCR）。
            resume_store: 断点存储（缺省按 ``process_dir`` + 票号新建）。
            progress_cb: 进度回调（每条 / 每阶段调用一次）。
            export: 完成后是否导出三产物。
            allowed_result_root: 成果产出根的校验用根（产物分流断言）。
            allowed_process_root: 过程产出根的校验用根。

        Returns:
            :class:`PipelineResult`。
        """
        excel = normalize_path(excel_path)
        share = normalize_path(share_root)
        result_path = normalize_path(result_dir)
        process_path = normalize_path(process_dir)

        emit = _make_emitter(progress_cb)

        # ══ Phase0/1：探查 ══
        probe_result = self._probe_records(excel, emit)
        resolved_ticket = (ticket_no or "").strip() or (probe_result.ticket_no or "").strip()
        records = list(probe_result.records)

        # ══ Phase2：图片三级索引匹配（干跑）══
        self._resolve_evidences(records, share, resolved_ticket, emit)

        # ══ 断点存储 ══
        fingerprint = self._build_fingerprint(
            resolved_ticket, excel, share, probe_result.variant.value, len(records)
        )
        store = resume_store if resume_store is not None else ResumeStore(
            build_resume_path(process_path, resolved_ticket),
            fingerprint,
            allowed_root=allowed_process_root if allowed_process_root is not None else process_path,
        )

        # ══ Phase3：分批 OCR + 判定（逐条落盘）══
        counts = {v.value: 0 for v in ALL_VERDICTS}
        unreachable_streak = 0
        pause_key = ""
        pause_reason = ""
        aborted = False

        begin = max(0, int(start_index))
        total = len(records)

        emit(
            PipelineProgress(
                phase="ocr",
                current=begin,
                total=total,
                counts=dict(counts),
                message=f"开始分批 OCR + 判定（共 {total} 条，从第 {begin + 1} 条起）",
            )
        )

        index = begin
        while index < total:
            # ── 记录边界：暂停检查（阻塞挂起，UI 进度冻结）──
            self._wait_if_paused(index, total, counts, emit)

            # ── 记录边界：中止检查 ──
            if self._stop_flag.is_set():
                aborted = True
                self._log.warning(f"收到中止信号，已在第 {index} 条边界退出，落盘已完成结果")
                break

            # ── 批次划分（按首条图片数决定批大小）──
            head = records[index]
            batch_len = self._effective_batch_size(head)
            batch = records[index : index + batch_len]

            batch_started = time.monotonic()
            for record in batch:
                # 批内每条也检查中止 / 暂停（保证响应及时）
                self._wait_if_paused(index, total, counts, emit)
                if self._stop_flag.is_set():
                    aborted = True
                    break

                key = self._safe_key(record)
                try:
                    result = self._process_record(record, self._effective_workers(record))
                except Exception as exc:  # noqa: BLE001 - 异常隔离：单条失败不中断整批
                    self._log.error(
                        f"第 {index + 1} 条处理异常（已隔离，继续下一批）："
                        f"{key} —— {type(exc).__name__}: {exc}"
                    )
                    emit(
                        PipelineProgress(
                            phase="ocr",
                            current=index,
                            total=total,
                            current_key=key,
                            counts=dict(counts),
                            message=f"第 {index + 1} 条处理异常，已隔离并继续：{key}",
                            level="ERROR",
                        )
                    )
                    # 不计入 counts（未完成），断点未标记 → 下次续跑补齐
                    continue

                # 逐条落盘（R3：防中途崩丢数据）
                _safe_mark_done(store, result, self._log)

                counts[result.verdict.value] = counts.get(result.verdict.value, 0) + 1
                index += 1

                # ── 禁令 1：连续 K 条 unreachable → 自动暂停（严禁用 NO_IMAGE 计数）──
                if result.unreachable:
                    unreachable_streak += 1
                else:
                    unreachable_streak = 0

                emit(
                    PipelineProgress(
                        phase="ocr",
                        current=index,
                        total=total,
                        current_key=key,
                        counts=dict(counts),
                        message=(
                            f"[{index}/{total}] {key or '-'} → "
                            f"{result.verdict.value}"
                            + ("（共享盘不可达）" if result.unreachable else "")
                        ),
                        level="WARN" if result.unreachable else "INFO",
                    )
                )

                if unreachable_streak >= self._unreachable_k:
                    pause_key = key
                    pause_reason = (
                        f"共享盘连接中断（连续 {unreachable_streak} 条不可达），"
                        f"已暂停在 {key}，恢复连接后点『继续执行』"
                    )
                    self._log.error(pause_reason)
                    emit(
                        PipelineProgress(
                            phase="ocr",
                            current=index,
                            total=total,
                            current_key=key,
                            counts=dict(counts),
                            message=pause_reason,
                            level="ERROR",
                        )
                    )
                    _safe_flush(store, self._log)
                    return self._finalize(
                        store=store,
                        counts=counts,
                        ticket=resolved_ticket,
                        total=total,
                        processed=index,
                        aborted=False,
                        failed=False,
                        paused=True,
                        pause_key=pause_key,
                        pause_reason=pause_reason,
                        notes=probe_result.notes,
                        result_path=result_path,
                        process_path=process_path,
                        export=False,
                        allowed_result_root=allowed_result_root,
                        allowed_process_root=allowed_process_root,
                        fingerprint=fingerprint,
                        emit=emit,
                    )

            if aborted:
                break

            # ── 批间隔：保活 + 让 UI 有机会重绘 ──
            batch_elapsed = time.monotonic() - batch_started
            emit(
                PipelineProgress(
                    phase="ocr",
                    current=index,
                    total=total,
                    counts=dict(counts),
                    message=f"批次完成（{len(batch)} 条，用时 {batch_elapsed:.1f}s），继续下一批",
                )
            )

        # ══ 落盘（中止路径也要 flush，已完成结果绝不丢）══
        _safe_flush(store, self._log)

        return self._finalize(
            store=store,
            counts=counts,
            ticket=resolved_ticket,
            total=total,
            processed=index,
            aborted=aborted,
            failed=False,
            paused=False,
            pause_key=pause_key,
            pause_reason=pause_reason,
            notes=probe_result.notes,
            result_path=result_path,
            process_path=process_path,
            export=export and not aborted,
            allowed_result_root=allowed_result_root,
            allowed_process_root=allowed_process_root,
            fingerprint=fingerprint,
            emit=emit,
        )

    # ══════════════════════════════════════════════════════════
    #  阶段实现
    # ══════════════════════════════════════════════════════════

    def _probe_records(
        self,
        excel: Path,
        emit: Callable[..., None],
    ) -> ExcelProbeResult:
        """Phase1：结构探查 + 解析，返回 :class:`core.models.ExcelProbeResult`。"""
        emit(PipelineProgress(phase="probe", message=f"探查 Excel 结构：{excel}"))
        result = self._probe.probe(excel)
        emit(
            PipelineProgress(
                phase="probe",
                total=len(result.records),
                message=(
                    f"变体 {result.variant.value}，要素表={result.sheet_name}，"
                    f"表头行(1-based)={result.header_row + 1}，"
                    f"票号={result.ticket_no}，记录数={len(result.records)}"
                ),
            )
        )
        for note in result.notes:
            emit(PipelineProgress(phase="probe", message=note, level="WARN"))
        return result

    def _resolve_evidences(
        self,
        records: list[DeclarationRecord],
        share: Path,
        ticket: str,
        emit: Callable[..., None],
    ) -> None:
        """Phase2：图片三级索引匹配（干跑，不跑 OCR）。"""
        total = len(records)
        emit(PipelineProgress(phase="resolve", total=total, message="图片三级索引匹配（干跑）"))
        hit = 0
        unreachable_count = 0
        for record in records:
            try:
                evidences = self._resolver.resolve(record, share, ticket_no=ticket)
            except Exception as exc:  # noqa: BLE001 - 匹配失败不中断（判 🔵）
                self._log.warning(f"图片匹配异常（按缺图处置）：{type(exc).__name__}: {exc}")
                evidences = []
            record.evidences = evidences
            if evidences:
                hit += 1
            if any(ev.unreachable for ev in evidences):
                unreachable_count += 1
        emit(
            PipelineProgress(
                phase="resolve",
                current=total,
                total=total,
                message=f"图片匹配完成：命中 {hit}/{total} 条记录"
                + (f"，其中 {unreachable_count} 条共享盘不可达" if unreachable_count else ""),
                level="WARN" if unreachable_count else "INFO",
            )
        )

    def _process_record(self, record: DeclarationRecord, max_workers: int) -> CheckResult:
        """处理单条记录：OCR（并行）→ 判定，返回 :class:`core.models.CheckResult`。

        Args:
            record: 申报记录（``evidences`` 已由 Phase2 填充）。
            max_workers: OCR 并发数。

        Returns:
            判定结果。
        """
        paths = [ev.image_path for ev in record.evidences if ev.image_path and ev.exists]
        if paths:
            ocr_texts = self._ocr.recognize_batch(paths, max_workers=max_workers)
            # 按路径回填到对应证据（等长同序）
            by_path = {t.image_path: t for t in ocr_texts}
            for ev in record.evidences:
                if ev.image_path in by_path:
                    ev.ocr = by_path[ev.image_path]
                    ev.ocr_confidence = float(by_path[ev.image_path].confidence or 0.0)
        return self._judge.judge(record)

    def _finalize(  # noqa: C901 - 收口逻辑，字段多
        self,
        *,
        store: ResumeStore,
        counts: dict[str, int],
        ticket: str,
        total: int,
        processed: int,
        aborted: bool,
        failed: bool,
        paused: bool,
        pause_key: str,
        pause_reason: str,
        notes: list[str],
        result_path: Path,
        process_path: Path,
        export: bool,
        allowed_result_root: str | os.PathLike[str] | None,
        allowed_process_root: str | os.PathLike[str] | None,
        fingerprint: Fingerprint,
        emit: Callable[..., None],
    ) -> PipelineResult:
        """收口：汇总结果、按需导出三产物、组装 :class:`PipelineResult`。"""
        results = store.accumulated_results()
        processed = len(results)

        output_paths: dict[str, str] = {}
        if export and results:
            exporter = self._exporter or ResultExporter(
                result_path,
                process_path,
                allowed_result_root=allowed_result_root if allowed_result_root is not None else result_path,
                allowed_process_root=allowed_process_root if allowed_process_root is not None else process_path,
            )
            emit(PipelineProgress(phase="export", current=processed, total=total,
                                  message="导出三产物（汇总表 / 复核清单 / 详细日志）"))
            try:
                paths = exporter.export_all(
                    results,
                    ticket,
                    extra={
                        "variant": fingerprint.variant,
                        "total": total,
                        "processed": processed,
                        "aborted": aborted,
                        "paused": paused,
                        "excel_hash": fingerprint.excel_hash,
                    },
                )
                output_paths = {k: str(v) for k, v in paths.items()}
                for name, path in output_paths.items():
                    emit(PipelineProgress(phase="export", message=f"{name} → {path}"))
            except CustomsCheckerError as exc:
                # 导出失败（越界 / IO）→ 致命，但已完成结果仍在断点里
                self._log.error(f"导出失败：{exc.user_message}")
                return PipelineResult(
                    ok=False,
                    aborted=aborted,
                    failed=True,
                    paused=paused,
                    results=results,
                    counts=dict(counts),
                    output_paths={},
                    ticket_no=ticket,
                    total=total,
                    processed=processed,
                    message=exc.user_message,
                    pause_key=pause_key,
                    pause_reason=pause_reason,
                    notes=list(notes),
                    excel_hash=fingerprint.excel_hash,
                )

        if paused:
            message = pause_reason
        elif aborted:
            message = (
                f"已中止：完成 {processed}/{total} 条，已完成结果已保存（断点可续跑）。"
            )
        elif failed:
            message = "跑批失败。"
        else:
            message = (
                f"跑批完成：共 {processed}/{total} 条，"
                f"✅{counts.get(Verdict.PASS.value, 0)} "
                f"❌{counts.get(Verdict.FAIL.value, 0)} "
                f"⚠️{counts.get(Verdict.NO_MARK.value, 0)} "
                f"🔵{counts.get(Verdict.NO_IMAGE.value, 0)}"
            )

        ok = not (aborted or failed or paused)
        emit(
            PipelineProgress(
                phase="finished" if ok else ("paused" if paused else ("aborted" if aborted else "failed")),
                current=processed,
                total=total,
                counts=dict(counts),
                message=message,
                level="INFO" if ok else "WARN",
            )
        )
        return PipelineResult(
            ok=ok,
            aborted=aborted,
            failed=failed,
            paused=paused,
            results=results,
            counts=dict(counts),
            output_paths=output_paths,
            ticket_no=ticket,
            total=total,
            processed=processed,
            message=message,
            pause_key=pause_key,
            pause_reason=pause_reason,
            notes=list(notes),
            excel_hash=fingerprint.excel_hash,
        )

    # ══════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════

    def _wait_if_paused(
        self,
        current: int,
        total: int,
        counts: dict[str, int],
        emit: Callable[..., None],
    ) -> None:
        """若 ``pause_flag`` 置位则在**记录边界**阻塞挂起，直到被清除。

        阻塞期间不消费 CPU（``wait`` 带超时轮询，便于及时响应中止）。
        """
        if not self._pause_flag.is_set():
            return
        emit(
            PipelineProgress(
                phase="paused",
                current=current,
                total=total,
                counts=dict(counts),
                message=f"已暂停在第 {current} 条边界，点『继续执行』可续跑",
                level="WARN",
            )
        )
        while self._pause_flag.is_set():
            if self._stop_flag.is_set():
                return
            self._pause_flag.wait(self._poll_interval)

    def _effective_batch_size(self, head: DeclarationRecord | None) -> int:
        """按首条图片数决定批大小（图片多降批，R3）。"""
        if head is None:
            return self._batch_size
        image_count = len([ev for ev in head.evidences if ev.image_path])
        if image_count >= self._heavy_threshold:
            return self._batch_size_heavy
        return self._batch_size

    def _effective_workers(self, record: DeclarationRecord) -> int:
        """按图片数决定 OCR 并发（≤2，R8）。"""
        image_count = len([ev for ev in record.evidences if ev.image_path and ev.exists])
        return 2 if image_count > 1 else 1

    @staticmethod
    def _safe_key(record: DeclarationRecord | None) -> str:
        """安全取记录 key（异常返回空串）。"""
        if record is None:
            return ""
        try:
            return record.key()
        except Exception:  # noqa: BLE001
            return ""

    def _build_fingerprint(
        self,
        ticket: str,
        excel: Path,
        share: Path,
        variant: str,
        total: int,
    ) -> Fingerprint:
        """构造断点指纹（供写入与匹配共用）。"""
        from infra.encoding import normalize_path_str

        return Fingerprint(
            ticket_no=(ticket or "").strip(),
            excel_path=normalize_path_str(excel),
            excel_hash=compute_excel_hash(excel),
            share_root=normalize_path_str(share),
            variant=str(variant or ""),
            total_count=int(total or 0),
        )


# ══════════════════════════════════════════════════════════════════
#  模块级辅助
# ══════════════════════════════════════════════════════════════════


def _make_emitter(cb: ProgressCallback | None) -> Callable[..., None]:
    """把回调包装为安全发射器（回调异常不影响跑批）。"""

    def emit(progress: PipelineProgress) -> None:
        if cb is None:
            return
        try:
            cb(progress)
        except Exception as exc:  # noqa: BLE001 - 进度回调故障不得中断跑批
            get_logger(Phase.PHASE3).debug(f"进度回调异常（已忽略）：{exc}")

    return emit


def _safe_mark_done(store: ResumeStore, result: CheckResult, log: object) -> None:
    """逐条落盘（同时标记完成 + 累积结果）。

    写盘失败记 ERROR（不吞），但**不中断整批** —— 下次续跑会补上该条。
    """
    try:
        store.mark_done(result.key, result)
    except Exception as exc:  # noqa: BLE001 - 落盘失败不应中断整批
        try:
            log.error(f"断点落盘失败（该条将在续跑时补齐）：{result.key} —— {exc}")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _safe_flush(store: ResumeStore, log: object) -> None:
    """安全 flush（失败记 ERROR，不抛）。"""
    try:
        store.flush()
    except Exception as exc:  # noqa: BLE001 - flush 失败不应中断收口
        try:
            log.error(f"断点 flush 失败：{exc}")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
