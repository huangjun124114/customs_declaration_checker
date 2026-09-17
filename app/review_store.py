"""人工改判存储（app.review_store，对应架构设计第 5 节链路 B + Q6 + R10）。

职责：

  * **幂等改判** —— 基于 key 的覆盖式赋值（同 key 重复调用以最后一次为准）；
  * **历史留痕** —— 每次保存生成一份 ``review_round_N.json``（过程产出）；
  * **复核后另存** —— 导出「复核后」JSON（``…_复核后.json``），**原始累积版不动**
    （Q6 可追溯红线）；
  * **回写结果集** —— 改判后把 ``verdict`` / ``reviewer_note`` / ``manually_reviewed``
    写回 :class:`core.models.CheckResult`。

**R10 数据完整性**：改判记录用 :class:`ReviewOverride` ``dataclass`` 承载
（**禁止**裸元组，防"元组取值 bug"导致复核清单「问题说明」以"零"开头）。

**分层约束**：``app/``（L3）只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.session import AppSession
from core.constants import VERDICT_TEXT
from core.models import Verdict
from core.result_exporter import ResultExporter
from infra.encoding import normalize_path
from infra.logger import Phase, get_logger

__all__ = [
    "REVIEWED_JSON_SUFFIX",
    "ROUND_JSON_PREFIX",
    "ReviewOverride",
    "ReviewStore",
]


#: 复核后 JSON 文件名后缀（保留原始累积版不动，Q6）
REVIEWED_JSON_SUFFIX: str = "_复核后.json"
#: 每轮留痕 JSON 文件名前缀
ROUND_JSON_PREFIX: str = "review_round"


@dataclass
class ReviewOverride:
    """单条改判记录（R10：结构化，禁止裸元组）。

    Attributes:
        key: 三级索引唯一键。
        verdict: 改判后的判定。
        note: 复核备注。
        round_index: 所属轮次（从 1 起）。
        timestamp: 改判时间（ISO 8601）。
        mark_missing: 是否「标记待补图」（🔵 缺图类的专用动作，Q7）。
        original_verdict: 【v0.3.7】**本次改判前**该条的系统判定值
            （``Verdict.value``；口径见 :attr:`core.models.CheckResult.original_verdict`）。
    """

    key: str = ""
    verdict: Verdict = Verdict.NO_MARK
    note: str = ""
    round_index: int = 1
    timestamp: str = ""
    mark_missing: bool = False
    original_verdict: str = ""

    def to_dict(self) -> dict[str, object]:
        """序列化为字典。"""
        return {
            "key": self.key,
            "verdict": self.verdict.value,
            "verdict_text": VERDICT_TEXT.get(self.verdict, self.verdict.value),
            "note": self.note,
            "round_index": self.round_index,
            "timestamp": self.timestamp,
            "mark_missing": self.mark_missing,
            "original_verdict": self.original_verdict,
            "original_verdict_text": self._original_text(),
        }

    def _original_text(self) -> str:
        """把 ``original_verdict``（值串）转成口径字符串（空串保持空串）。"""
        if not self.original_verdict:
            return ""
        try:
            return VERDICT_TEXT.get(Verdict(self.original_verdict), self.original_verdict)
        except ValueError:  # 未知取值 → 原样回显，不抛
            return self.original_verdict

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ReviewOverride:
        """从字典构造（容忍缺失字段）。"""
        data = data or {}
        raw_verdict = str(data.get("verdict", Verdict.NO_MARK.value) or "")
        try:
            verdict = Verdict(raw_verdict)
        except ValueError:
            verdict = Verdict.NO_MARK
        return cls(
            key=str(data.get("key", "") or ""),
            verdict=verdict,
            note=str(data.get("note", "") or ""),
            round_index=int(data.get("round_index", 1) or 1),
            timestamp=str(data.get("timestamp", "") or ""),
            mark_missing=bool(data.get("mark_missing", False)),
            original_verdict=str(data.get("original_verdict", "") or ""),
        )


class ReviewStore:
    """人工改判存储（架构设计第 4 节 ``ReviewStore``）。

    Args:
        session: 会话结果集（改判会回写其中对应记录）。
        process_dir: 过程产出目录（留痕 JSON 与「复核后」JSON 落点）。
        allowed_root: 允许写入的根（过程产出根）；给定则写前断言归属。
    """

    def __init__(
        self,
        session: AppSession,
        process_dir: str | os.PathLike[str],
        *,
        allowed_root: str | os.PathLike[str] | None = None,
    ) -> None:
        """构造改判存储。"""
        self._session = session
        self._process_dir = normalize_path(process_dir)
        self._allowed_root: Path | None = (
            normalize_path(allowed_root) if allowed_root is not None else None
        )
        self._log = get_logger(Phase.PHASE6)

        #: ``{key: ReviewOverride}`` 当前生效的改判（幂等覆盖）
        self._overrides: dict[str, ReviewOverride] = {}
        #: 每轮留痕（``[{round_index: int, records: [ReviewOverride, ...]}]``）
        self._rounds: list[dict[str, object]] = []
        self._round_index: int = 0

    # ══════════════════════════════════════════════════════════
    #  改判（幂等）
    # ══════════════════════════════════════════════════════════

    def set_verdict(
        self,
        key: str,
        verdict: Verdict | str,
        note: str = "",
        *,
        mark_missing: bool = False,
    ) -> ReviewOverride:
        """改判某条结果（**幂等**：同 key 重复调用以最后一次为准）。

        Args:
            key: 三级索引唯一键。
            verdict: 改判后的判定（枚举或字符串值）。
            note: 复核备注。
            mark_missing: 是否「标记待补图」（🔵 缺图类专用动作）。

        Returns:
            生效的 :class:`ReviewOverride`。

        Raises:
            KeyError: 结果集中不存在该 key。
        """
        normalized = (key or "").strip()
        if not normalized:
            raise KeyError("key 不能为空")
        result = self._session.get(normalized)
        if result is None:
            raise KeyError(f"结果集中不存在该记录：{normalized}")

        resolved = verdict if isinstance(verdict, Verdict) else Verdict(str(verdict))
        # 【v0.3.7 需求 4】「重判结果不覆盖原系统产生的结果」：
        # 首次改判时把**系统判定**原样记下；后续改判**不得**改写它
        # （否则 original 会退化成"上一次的重判值"，可追溯性失真）。
        original: Verdict | None = getattr(result, "original_verdict", None)
        if original is None:
            original = result.verdict
            result.original_verdict = original

        override = ReviewOverride(
            key=normalized,
            verdict=resolved,
            note=(note or "").strip(),
            round_index=self._round_index + 1,
            timestamp=_now_iso(),
            mark_missing=bool(mark_missing),
            original_verdict=original.value,
        )
        # 幂等：同 key 覆盖
        self._overrides[normalized] = override

        # 回写结果集（verdict / 备注 / 已复核标记 / **原系统判定留痕**）
        result.verdict = resolved
        result.reviewer_note = override.note
        result.manually_reviewed = True
        result.reviewed_at = override.timestamp
        self._log.info(
            f"改判：{normalized} → {VERDICT_TEXT.get(resolved, resolved.value)}"
            + f"（原系统判定：{VERDICT_TEXT.get(original, original.value)}）"
            + (f"（备注：{override.note}）" if override.note else "")
        )
        return override

    def get(self, key: str) -> ReviewOverride | None:
        """取某 key 当前生效的改判（未改判返回 ``None``）。"""
        return self._overrides.get((key or "").strip())

    def overrides(self) -> dict[str, ReviewOverride]:
        """返回全部改判（副本）。"""
        return dict(self._overrides)

    def override_count(self) -> int:
        """已改判条数。"""
        return len(self._overrides)

    def has_overrides(self) -> bool:
        """是否存在任何改判。"""
        return bool(self._overrides)

    # ══════════════════════════════════════════════════════════
    #  留痕 + 复核后另存（Q6）
    # ══════════════════════════════════════════════════════════

    def save_round(self) -> Path:
        """把当前改判集合另存为 ``review_round_N.json``（每轮一次，留痕）。

        Returns:
            落盘路径 :class:`pathlib.Path`。

        Raises:
            OutputPathViolation: 落点越界。
        """
        self._round_index += 1
        # 同轮次内记录的 round_index 修正为本轮号
        records = []
        for override in sorted(self._overrides.values(), key=lambda o: o.key):
            override.round_index = self._round_index
            records.append(override.to_dict())

        payload: dict[str, object] = {
            "schema": 1,
            "round": self._round_index,
            "ticket_no": self._session.ticket_no,
            "saved_at": _now_iso(),
            "override_count": len(records),
            "records": records,
        }
        self._rounds.append(payload)

        target = self._round_dir() / f"{ROUND_JSON_PREFIX}_{self._round_index}.json"
        self._write_json(target, payload)
        self._log.info(f"改判留痕已保存：{target}（第 {self._round_index} 轮）")
        return target

    def save_reviewed_json(
        self,
        exporter: ResultExporter,
        *,
        extra: dict[str, object] | None = None,
    ) -> Path:
        """另存「复核后」JSON（**原始累积版不动**，Q6）。

        落点：``{过程产出}/{票号}_复核后.json``。内容为改判后的完整结果集
        （含复核备注 + 改判历史）。

        Args:
            exporter: 结果导出器（提供 ``process_dir``）。
            extra: 附加元信息。

        Returns:
            落盘路径 :class:`pathlib.Path`。
        """
        ticket = self._session.ticket_no or "UNKNOWN"
        target = exporter.process_dir / f"{_safe_name(ticket)}{REVIEWED_JSON_SUFFIX}"

        results = self._session.results()
        payload: dict[str, object] = {
            "schema": 1,
            "ticket_no": ticket,
            "generated_at": _now_iso(),
            "manually_reviewed": True,
            "override_count": len(self._overrides),
            "overrides": [o.to_dict() for o in sorted(self._overrides.values(), key=lambda x: x.key)],
            "total": len(results),
            "meta": extra or {},
            "results": [r.to_dict() for r in results],
        }
        self._write_json(target, payload)
        self._log.info(f"复核后 JSON 已另存（原始累积版保留不动）：{target}")
        return target

    # ══════════════════════════════════════════════════════════
    #  保存复核即落盘（v0.3.7 需求 4）
    # ══════════════════════════════════════════════════════════

    def persist(self, exporter: ResultExporter) -> dict[str, Path]:
        """**保存复核即落盘**：一次调用把「留痕 + 复核后版本」全部写到磁盘。

        四件事（顺序固定，任一步失败即上抛，由 UI 明确提示而非静默）：

          1. ``复核留痕/review_round_N.json`` —— 本轮留痕（**每保存一次即一轮**，
             append-only，便于回溯"什么时候改过哪几条"）；
          2. ``{票号}_复核后.json`` —— 复核后完整结果集（原始累积版**不动**，Q6）；
          3. ``校验汇总表_{票号}_复核后.xlsx`` —— 复核后汇总表
             （13 列 + 「人工复核 / 原系统判定」两列标识）；
          4. ``{票号}_待人工复核清单_复核后.csv`` —— 复核后复核清单（同追加两列）。

        ⚠️ **红线：绝不覆盖系统原始产物**。全部落点都是另存文件
        （``_复核后`` 中缀），原始 ``校验汇总表_{票号}.xlsx`` /
        ``校验详细日志_{票号}_累积.json`` 保持系统跑批时那一刻的内容不变 ——
        复核人可随时对照"系统判成什么 / 人工改成什么"。

        Args:
            exporter: 结果导出器（提供成果产出 / 过程产出目录与越界断言）。

        Returns:
            ``{"round": Path, "reviewed_json": Path,
            "summary_reviewed": Path, "review_reviewed": Path}``。
        """
        round_path = self.save_round()
        reviewed_json = self.save_reviewed_json(exporter)
        artifacts = exporter.export_reviewed_all(
            self._session.results(), self._session.ticket_no
        )
        self._log.info(
            "复核已落盘（原系统产物未改动）："
            f"汇总表={artifacts['summary_reviewed']}｜"
            f"复核清单={artifacts['review_reviewed']}｜"
            f"复核后 JSON={reviewed_json}"
        )
        return {
            "round": round_path,
            "reviewed_json": reviewed_json,
            "summary_reviewed": artifacts["summary_reviewed"],
            "review_reviewed": artifacts["review_reviewed"],
        }

    # ══════════════════════════════════════════════════════════
    #  重置
    # ══════════════════════════════════════════════════════════

    def reset(self) -> None:
        """清空改判记录与轮次（新一轮跑批时调用）。"""
        self._overrides.clear()
        self._rounds.clear()
        self._round_index = 0

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════

    def _round_dir(self) -> Path:
        """返回留痕目录 ``{过程产出}/复核留痕``。"""
        return self._process_dir / "复核留痕"

    def _write_json(self, target: Path, payload: dict[str, object]) -> None:
        """写 JSON（先断言归属，再落盘）。

        Raises:
            OutputPathViolation: 落点越界。
        """
        if self._allowed_root is not None:
            from infra.fs_lock import assert_within

            assert_within(target, self._allowed_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._log.error(f"改判留痕写入失败：{target}（{exc}）")
            raise


def _now_iso() -> str:
    """返回当前时间 ISO 8601 字符串（秒级）。"""
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _safe_name(ticket: str) -> str:
    """清洗票号中的非法文件名字符。"""
    raw = (ticket or "").strip()
    cleaned = "".join(ch for ch in raw if ch not in '\\/:*?"<>|')
    return cleaned or "UNKNOWN"
