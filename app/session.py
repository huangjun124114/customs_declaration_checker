"""会话结果集（app.session，对应架构设计第 4 节 ``AppSession``）。

:class:`AppSession` 在**内存中持有**一次会话的全部 ``CheckResult``，供 UI 查询：

  * 四类判定计数（供结果总览四卡）；
  * 待复核队列（``REVIEW_VERDICTS`` 筛选，供人工复核工作台）；
  * 单条结果按 key 查询（供工作台卡片渲染）；
  * 结果集替换 / 累积（跑批过程中逐条并入）。

**线程安全**：内部用 ``threading.Lock`` 保护（Worker 线程写入，UI 线程读取）。

**分层约束**：``app/``（L3）只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import threading

from core.constants import ALL_VERDICTS, REVIEW_VERDICTS
from core.models import CheckResult, Verdict

__all__ = ["AppSession"]


class AppSession:
    """一次会话的结果集持有者（线程安全）。

    典型用法::

        session = AppSession()
        session.replace(results)          # 跑批完成后整体替换
        session.upsert(result)            # 或逐条并入
        f"pending: {len(session.pending_results())}"
    """

    def __init__(self) -> None:
        """构造空会话。"""
        self._lock = threading.Lock()
        self._results: dict[str, CheckResult] = {}
        self._order: list[str] = []
        self._ticket_no: str = ""

    # ══════════════════════════════════════════════════════════
    #  写入
    # ══════════════════════════════════════════════════════════

    def replace(self, results: list[CheckResult], *, ticket_no: str = "") -> None:
        """用新结果集整体替换当前会话（保持输入顺序）。

        Args:
            results: 校验结果列表。
            ticket_no: 本次票号。
        """
        with self._lock:
            self._results.clear()
            self._order.clear()
            if ticket_no:
                self._ticket_no = ticket_no.strip()
            for result in results or []:
                key = result.key or self._fallback_key(result)
                if key and key not in self._results:
                    self._order.append(key)
                if key:
                    self._results[key] = result

    def upsert(self, result: CheckResult) -> None:
        """并入 / 覆盖单条结果（幂等：同 key 以最后一次为准）。

        Args:
            result: 校验结果。
        """
        if result is None:
            return
        key = result.key or self._fallback_key(result)
        if not key:
            return
        with self._lock:
            if key not in self._results:
                self._order.append(key)
            self._results[key] = result

    def clear(self) -> None:
        """清空结果集（新一轮跑批「重新开始」时调用）。"""
        with self._lock:
            self._results.clear()
            self._order.clear()

    def set_ticket_no(self, ticket_no: str) -> None:
        """设置当前票号。"""
        with self._lock:
            self._ticket_no = (ticket_no or "").strip()

    # ══════════════════════════════════════════════════════════
    #  读取
    # ══════════════════════════════════════════════════════════

    @property
    def ticket_no(self) -> str:
        """当前票号。"""
        with self._lock:
            return self._ticket_no

    def results(self) -> list[CheckResult]:
        """返回全部结果（按加入顺序）。"""
        with self._lock:
            return [self._results[k] for k in self._order if k in self._results]

    def get(self, key: str) -> CheckResult | None:
        """按 key 查询单条结果。

        Args:
            key: 三级索引唯一键。

        Returns:
            命中的 :class:`core.models.CheckResult`；未命中返回 ``None``。
        """
        with self._lock:
            return self._results.get(key or "")

    def __len__(self) -> int:
        """结果条数。"""
        with self._lock:
            return len(self._results)

    def counts(self) -> dict[str, int]:
        """返回四类判定计数（键恒为四类判定，值为整数）。

        Returns:
            ``{Verdict.value: int}``；未出现的判定为 0。
        """
        tally = {verdict.value: 0 for verdict in ALL_VERDICTS}
        with self._lock:
            for result in self._results.values():
                tally[result.verdict.value] = tally.get(result.verdict.value, 0) + 1
        return tally

    def count_sum(self) -> int:
        """四类判定计数之和（应等于结果条数，FR-006/008 断言用）。"""
        return sum(self.counts().values())

    def pending_results(self) -> list[CheckResult]:
        """返回**待人工复核**队列（``REVIEW_VERDICTS`` = ⚠️ + 🔵）。

        Returns:
            待复核的 :class:`core.models.CheckResult` 列表（按加入顺序）。
        """
        with self._lock:
            return [
                self._results[k]
                for k in self._order
                if k in self._results and self._results[k].verdict in REVIEW_VERDICTS
            ]

    def pending_count(self) -> int:
        """待人工复核条数。"""
        return len(self.pending_results())

    def results_by_verdict(self, verdict: Verdict) -> list[CheckResult]:
        """返回指定判定的全部结果。

        Args:
            verdict: 判定枚举。

        Returns:
            匹配的 :class:`core.models.CheckResult` 列表。
        """
        with self._lock:
            return [
                self._results[k]
                for k in self._order
                if k in self._results and self._results[k].verdict == verdict
            ]

    @staticmethod
    def _fallback_key(result: CheckResult) -> str:
        """当 ``result.key`` 为空时，从 ``record`` 现算 key（兜底）。"""
        record = getattr(result, "record", None)
        if record is None:
            return ""
        try:
            return record.key()
        except Exception:  # noqa: BLE001 - 兜底路径不得抛异常
            return ""
