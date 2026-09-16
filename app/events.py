"""跑批进度事件与断点摘要（app.events，对应架构设计第 4 节 + 12.A.4）。

本模块定义**跨线程传输的事件对象**（纯 ``dataclass``，无 Qt 依赖）：

  * :class:`TaskPhase` —— 跑批阶段枚举（对齐 SOP 8 Phase + 12.A 状态机）；
  * :class:`TaskProgress` —— 单条进度事件（阶段 / 当前条 / 总数 / 当前 key /
    四类计数 / 日志行 / 级别），由 ``RunController.progress`` 信号携带；
  * :class:`BreakpointSummary` —— 断点摘要（``done_count`` / ``total_count`` /
    ``last_ts`` / ``matched`` / ``ticket_no``），驱动「开始→重新开始」「暂停→继续执行」
    与预检行红字（架构设计 12.A.1/A.2）。

**分层约束**：本模块属 ``app/``（L3），只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.models import LogLevel

__all__ = [
    "TaskPhase",
    "TaskProgress",
    "BreakpointSummary",
    "PipelineOutcome",
]


class TaskPhase(str, Enum):
    """跑批阶段（驱动 UI 状态显示与按钮状态机）。"""

    IDLE = "IDLE"                          # 未开始
    PREFLIGHT = "PREFLIGHT"                # Phase0 预检
    PROBE = "PROBE"                        # Phase1 结构探查
    PARSE = "PARSE"                        # Phase1 解析
    RESOLVE = "RESOLVE"                    # Phase2 图片匹配（干跑）
    OCR = "OCR"                            # Phase3 分批 OCR + 判定
    EXPORT = "EXPORT"                      # Phase7 导出
    FINISHED = "FINISHED"                  # 完成
    PAUSED = "PAUSED"                      # 已暂停（保留断点）
    ABORTED = "ABORTED"                    # 已中止（保留已完成结果）
    FAILED = "FAILED"                      # 致命失败（已中止跑批）


#: 阶段的中文展示名（UI 状态栏 / 日志区）
PHASE_LABELS: dict[TaskPhase, str] = {
    TaskPhase.IDLE: "空闲",
    TaskPhase.PREFLIGHT: "数据源预检",
    TaskPhase.PROBE: "结构探查",
    TaskPhase.PARSE: "申报要素解析",
    TaskPhase.RESOLVE: "图片三级索引匹配",
    TaskPhase.OCR: "分批 OCR + 判定",
    TaskPhase.EXPORT: "三产物导出",
    TaskPhase.FINISHED: "已完成",
    TaskPhase.PAUSED: "已暂停",
    TaskPhase.ABORTED: "已中止",
    TaskPhase.FAILED: "已失败",
}


@dataclass
class TaskProgress:
    """单条进度事件（``RunController.progress`` 信号携带）。

    Attributes:
        phase: 当前阶段。
        current: 已处理条数（当前条序号，从 0 起）。
        total: 总条数。
        current_key: 当前处理的记录 key（三级索引唯一键）。
        counts: 四类判定计数 ``{Verdict.value: int}``；键恒为四类判定。
        message: 人类可读的进度消息（进日志区 / 状态栏）。
        level: 日志级别（``INFO`` / ``WARN`` / ``ERROR``，UI 侧据此过滤）。
        fraction: 进度比例（``0.0–1.0``）；``total <= 0`` 时为 ``0.0``。
        extra: 附加结构化信息（如 ``{"unreachable_streak": 3}``）。
    """

    phase: TaskPhase = TaskPhase.IDLE
    current: int = 0
    total: int = 0
    current_key: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    message: str = ""
    level: LogLevel = LogLevel.INFO
    fraction: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        phase: TaskPhase,
        *,
        current: int = 0,
        total: int = 0,
        current_key: str = "",
        counts: dict[str, int] | None = None,
        message: str = "",
        level: LogLevel = LogLevel.INFO,
        extra: dict[str, Any] | None = None,
    ) -> TaskProgress:
        """构造一条进度事件（自动计算 ``fraction``）。

        Args:
            phase: 当前阶段。
            current: 已处理条数。
            total: 总条数。
            current_key: 当前记录 key。
            counts: 四类判定计数。
            message: 进度消息。
            level: 日志级别。
            extra: 附加结构化信息。

        Returns:
            :class:`TaskProgress` 实例。
        """
        safe_total = max(0, int(total))
        safe_current = max(0, int(current))
        if safe_total > 0:
            fraction = min(1.0, safe_current / float(safe_total))
        else:
            fraction = 0.0
        return cls(
            phase=phase,
            current=safe_current,
            total=safe_total,
            current_key=current_key or "",
            counts=dict(counts or {}),
            message=message or "",
            level=level,
            fraction=fraction,
            extra=dict(extra or {}),
        )

    @property
    def processed(self) -> int:
        """已处理条数（``current`` 的别名，语义更清晰）。"""
        return self.current

    @property
    def count_sum(self) -> int:
        """四类判定计数之和（FR-006/008 断言：应等于已处理数）。"""
        return sum(int(v) for v in self.counts.values())

    def progress_text(self) -> str:
        """返回 ``"N/M 条"`` 文本（进度区展示，FR-006）。"""
        return f"{self.current}/{self.total} 条"

    def phase_label(self) -> str:
        """返回当前阶段的中文名。"""
        return PHASE_LABELS.get(self.phase, self.phase.value)


@dataclass
class BreakpointSummary:
    """断点摘要（``RunController.breakpoint_detected`` 信号携带，架构设计 12.A.2）。

    Attributes:
        matched: 断点是否与当前数据源匹配（不匹配时不显示红字）。
        ticket_no: 断点所属票号。
        done_count: 已完成条数。
        total_count: 预计总条数。
        last_ts: 断点最后更新时间（ISO 8601；红字显示 ``HH:MM`` 片段）。
        resume_path: 断点文件路径（字符串，便于日志）。
        done_keys: 已完成的 key 集合（供续跑跳过）。
    """

    matched: bool = False
    ticket_no: str = ""
    done_count: int = 0
    total_count: int = 0
    last_ts: str = ""
    resume_path: str = ""
    done_keys: set[str] = field(default_factory=set)

    @property
    def has_unfinished(self) -> bool:
        """是否存在**未完成**的匹配断点（触发「重新开始 + 继续执行 + 红字」）。"""
        return bool(self.matched and 0 < self.done_count < max(self.total_count, 1))

    def short_ts(self) -> str:
        """返回 ``HH:MM`` 形式的短时间戳（从 ISO 串截取；失败返回原文）。"""
        text = (self.last_ts or "").strip()
        if "T" in text:
            tail = text.split("T", 1)[1]
            return tail[:5]
        if len(text) >= 5 and ":" in text:
            return text[-5:] if text.count(":") >= 2 else text
        return text or "-"

    def hint_text(self) -> str:
        """返回预检行红字文案（**精确模板**，架构设计 12.A.2）。

        Returns:
            形如
            ``⚠ 检测到未完成断点：已完成 23/64 条（上次中断于 12:15），可点『继续执行』从断点续跑``
            的红字文案；无未完成断点时返回空串。
        """
        if not self.has_unfinished:
            return ""
        return (
            f"⚠ 检测到未完成断点：已完成 {self.done_count}/{self.total_count} 条"
            f"（上次中断于 {self.short_ts()}），可点『继续执行』从断点续跑"
        )


@dataclass
class PipelineOutcome:
    """一次跑批的最终结果摘要（``RunController.finished`` 信号携带）。

    Attributes:
        ok: 是否正常完成（未被中止 / 未致命失败）。
        aborted: 是否被用户中止。
        failed: 是否致命失败。
        paused: 是否因自动暂停而停下（共享盘断连）。
        results: 全部校验结果（已完成部分）。
        counts: 四类判定计数。
        output_paths: 三产物落盘路径 ``{"summary"/"review"/"detail": str}``。
        ticket_no: 票号。
        total: 计划处理的记录总数。
        processed: 实际完成条数。
        message: 面向用户的总结文案。
        pause_key: 自动暂停时的记录 key（供红字提示）。
        pause_reason: 自动暂停原因文案。
        notes: 附加说明（如变体降级提示）。
    """

    ok: bool = True
    aborted: bool = False
    failed: bool = False
    paused: bool = False
    results: list[Any] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    ticket_no: str = ""
    total: int = 0
    processed: int = 0
    message: str = ""
    pause_key: str = ""
    pause_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def count_sum(self) -> int:
        """四类判定计数之和。"""
        return sum(int(v) for v in self.counts.values())
