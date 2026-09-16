"""数据源预检 + 断点探测（app.preflight，对应 SOP Phase 0 + 架构设计 12.A.4）。

包含两个组件：

  * :class:`PreflightRunner` —— 轻量预检：校验输入路径 + 结构探查一次（**不跑 OCR**），
    产出 :class:`app.path_policy.PreflightReport`（含票号 / 变体 / 记录数 / 提示）。
  * :class:`BreakpointProbe` —— 薄封装 ``ResumeStore.load_if_match()``，组装
    :class:`app.events.BreakpointSummary`，驱动「开始→重新开始」「暂停→继续执行」
    与预检行红字（架构设计 12.A.4）。

**分层约束**：``app/``（L3）只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.events import BreakpointSummary
from app.path_policy import PathPolicy, PreflightReport
from core.element_parser import ElementParser
from core.excel_probe import ExcelProbe
from core.models import ExcelProbeResult, Fingerprint
from core.resume_store import ResumeStore, build_resume_path, compute_excel_hash
from core.rule_repository import RuleRepository
from infra.errors import (
    CustomsCheckerError,
    PathNotAccessibleError,
    TicketNoNotFoundError,
    user_message_of,
)
from infra.logger import Phase, get_logger

__all__ = ["PreflightRunner", "BreakpointProbe"]


class PreflightRunner:
    """SOP Phase 0 预检执行器。

    预检内容（E5「启动即预检」，轻量、不跑 OCR）：

      1. 路径存在性 / 可读性 / UNC 可达性（交给 :class:`app.path_policy.PathPolicy`）；
      2. Excel 结构探查一次（变体 / 票号 / 表头 / 记录数）；
      3. 票号缺失 → 提示用户手填（Q9）。

    Args:
        rule_repository: 规则仓库（缺省新建并 ``load_all``）。
        path_policy: 路径策略（缺省新建）。
    """

    def __init__(
        self,
        rule_repository: RuleRepository | None = None,
        path_policy: PathPolicy | None = None,
    ) -> None:
        """构造预检执行器。"""
        self._repo = rule_repository if rule_repository is not None else RuleRepository()
        if rule_repository is None:
            self._repo.load_all()
        self._policy = path_policy if path_policy is not None else PathPolicy()
        self._log = get_logger(Phase.PHASE0)

    @property
    def rule_repository(self) -> RuleRepository:
        """当前规则仓库。"""
        return self._repo

    def run(
        self,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        *,
        ticket_no: str = "",
    ) -> PreflightReport:
        """执行预检（**永不抛异常**，失败信息装进 :class:`PreflightReport`）。

        Args:
            excel_path: 申报要素 Excel 路径。
            share_root: 图片根目录。
            ticket_no: 用户手填的票号（可选）。

        Returns:
            :class:`app.path_policy.PreflightReport`。
        """
        report = self._policy.validate_inputs(excel_path, share_root, ticket_no=ticket_no)
        if not report.ok:
            return report

        # ── 结构探查（轻量，不跑 OCR）──
        try:
            probe = self._probe(excel_path)
        except CustomsCheckerError as exc:
            report.ok = False
            report.errors.append(exc.user_message)
            return report
        except Exception as exc:  # noqa: BLE001 - 预检须兜住一切，禁止闪退
            report.ok = False
            report.errors.append(user_message_of(exc))
            return report

        report.variant = probe.variant.value
        report.sheet_name = probe.sheet_name
        report.header_row = probe.header_row
        report.record_estimate = len(probe.records)
        report.notes = list(probe.notes)

        # 变体降级提示（架构设计 13.13.1：notes 汇入预检提示）
        for note in probe.notes:
            report.warnings.append(note)

        # 票号：用户手填优先；否则用探查结果
        user_ticket = (ticket_no or "").strip()
        report.ticket_hint = user_ticket or (probe.ticket_no or "").strip()
        if not report.ticket_hint:
            report.ok = False
            report.errors.append(
                TicketNoNotFoundError().user_message
            )
            self._log.warning("预检：未能识别票号，要求用户手填")
            return report

        # 记录数为 0 → 视为致命（表里没有可校验的行）
        if report.record_estimate <= 0:
            report.ok = False
            report.errors.append(
                f"未从 Excel 解析到任何申报记录（要素表「{probe.sheet_name}」）。\n"
                "请确认所选文件正确，或反馈给实施人员补充结构适配。"
            )
            return report

        self._log.info(
            f"预检完成：变体={report.variant} 要素表={report.sheet_name} "
            f"表头行(0-based)={report.header_row} 票号={report.ticket_hint} "
            f"记录数={report.record_estimate}"
        )
        return report

    def probe_only(self, excel_path: str | os.PathLike[str]) -> ExcelProbeResult:
        """仅做结构探查（供 UI 单独调用，返回探查结果）。

        Args:
            excel_path: Excel 路径。

        Returns:
            :class:`core.models.ExcelProbeResult`。
        """
        return self._probe(excel_path)

    def _probe(self, excel_path: str | os.PathLike[str]) -> ExcelProbeResult:
        """构造 ``ExcelProbe`` 并执行探查。"""
        parser = ElementParser(self._repo)
        probe = ExcelProbe(parser=parser)
        return probe.probe(self._policy.clean_dialog_path(excel_path))


class BreakpointProbe:
    """断点探测器（架构设计 12.A.4）。

    读取 ``过程产出/断点/resume_{票号}.json``，仅当**指纹匹配**当前数据源时
    返回 :class:`app.events.BreakpointSummary`；否则返回「未匹配」摘要
    （``matched=False``，UI **不显示红字**，仅记 INFO 日志）。

    Args:
        process_dir: 过程产出目录（断点文件所在）。
        rule_repository: 规则仓库（计算指纹变体时使用，可缺省）。
    """

    def __init__(
        self,
        process_dir: str | os.PathLike[str],
        rule_repository: RuleRepository | None = None,
    ) -> None:
        """构造断点探测器。"""
        self._process_dir = str(process_dir)
        self._repo = rule_repository
        self._log = get_logger(Phase.PHASE0)

    def build_fingerprint(
        self,
        *,
        ticket_no: str,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        variant: str,
        total_count: int = 0,
    ) -> Fingerprint:
        """构造当前数据源指纹（供匹配与写入断点共用）。

        Args:
            ticket_no: 票号。
            excel_path: Excel 路径。
            share_root: 图片根（票号目录层）。
            variant: 结构变体字符串。
            total_count: 记录总数。

        Returns:
            :class:`core.models.Fingerprint`。
        """
        policy = PathPolicy()
        return Fingerprint(
            ticket_no=(ticket_no or "").strip(),
            excel_path=policy.normalize_key_path(excel_path),
            excel_hash=compute_excel_hash(excel_path),
            share_root=policy.normalize_key_path(share_root),
            variant=str(variant or ""),
            total_count=int(total_count or 0),
        )

    def probe(
        self,
        fingerprint: Fingerprint,
        ticket_no: str,
    ) -> BreakpointSummary:
        """探测断点并组装摘要（**永不抛异常**）。

        Args:
            fingerprint: 当前数据源指纹。
            ticket_no: 票号（用于定位断点文件）。

        Returns:
            :class:`app.events.BreakpointSummary`；无匹配断点时 ``matched=False``。
        """
        summary = BreakpointSummary(
            matched=False,
            ticket_no=(ticket_no or "").strip(),
            total_count=int(fingerprint.total_count or 0),
        )
        if not summary.ticket_no:
            return summary

        resume_path = build_resume_path(self._process_dir, summary.ticket_no)
        summary.resume_path = str(resume_path)
        if not resume_path.is_file():
            return summary

        try:
            store = ResumeStore(
                resume_path, fingerprint, allowed_root=self._process_dir or None
            )
            snapshot = store.load_if_match()
        except Exception as exc:  # noqa: BLE001 - 探测失败不得影响启动
            self._log.warning(f"断点探测失败（已忽略）：{exc}")
            return summary

        if snapshot is None:
            # 非匹配断点：不显示红字，load_if_match 内部已记 INFO
            self._log.info(
                f"发现 {ticket_no} 的断点，与当前数据源不匹配，已忽略"
            )
            return summary

        summary.matched = True
        summary.done_count = snapshot.done_count
        summary.last_ts = snapshot.updated_at
        summary.done_keys = set(snapshot.done_keys)
        if not summary.total_count:
            summary.total_count = int(snapshot.fingerprint.total_count or 0)
        self._log.info(
            f"断点匹配：已完成 {summary.done_count}/{summary.total_count} 条"
        )
        return summary


def ensure_share_dir(path: str | os.PathLike[str]) -> Path:
    """断言共享目录可达（辅助函数，供 UI 预检前调用）。

    Args:
        path: 目录路径。

    Returns:
        规范化后的 :class:`pathlib.Path`。

    Raises:
        PathNotAccessibleError: 不可达。
    """
    policy = PathPolicy()
    if not policy.probe_unc(path):
        raise PathNotAccessibleError(f"share root unreachable: {path}", path=str(path))
    return policy.clean_dialog_path(path)
