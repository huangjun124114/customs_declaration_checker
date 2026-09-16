"""断点 + 累积存储（core.resume_store，对应架构设计 12.A.3 / 12.A.4 / Phase3 R3）。

**核心设计目标**：**防串票**。

断点（``resume_{票号}.json``）升级为 **v2 复合结构**，携带「数据源指纹」
:class:`core.models.Fingerprint`。只有当**票号 / excel_hash / share_root / variant
全部匹配**时，断点才可复用；否则 :meth:`ResumeStore.load_if_match` 返回 ``None``
（**忽略，绝不静默混用**）。

文件结构（v2）::

    {
      "schema": 2,
      "fingerprint": {
        "ticket_no": "SA26090215",
        "excel_path": "D:\\\\...\\\\申报要素-SA26090215委内瑞拉.XLSX",
        "excel_hash": "sha256:1a2b3c...",
        "share_root": "\\\\\\\\172.20.99.220\\\\...",
        "variant": "D",
        "total_count": 18,
        "done_count": 23
      },
      "done_keys": ["SA26090215|N011901-007386-001|2660326M", "..."],
      "updated_at": "2026-09-16T12:15:03"
    }

**幂等**：同一 key 重复 ``mark_done`` / ``accumulate`` 以**最后一次**为准；
中断后重跑 ``done_keys`` **不回退**（只增不减，直到 :meth:`reset`）。

**异常不许吞**：写失败抛异常或记 ERROR（R3 要求"逐条落盘"可靠性）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from core.models import CheckResult, Fingerprint, ResumeSnapshot
from infra.encoding import file_sha256, normalize_path, normalize_path_str
from infra.fs_lock import assert_within
from infra.logger import Phase, get_logger

__all__ = [
    "SCHEMA_VERSION",
    "EXCEL_HASH_PREFIX",
    "compute_excel_hash",
    "build_resume_path",
    "ResumeStore",
]


#: 断点文件 schema 版本（v2：携带 fingerprint）
SCHEMA_VERSION: int = 2

#: Excel 哈希前缀（人类可读地标识算法）
EXCEL_HASH_PREFIX: str = "sha256:"


def compute_excel_hash(excel_path: str | os.PathLike[str]) -> str:
    """计算 Excel 文件的 sha256（**流式读取，只读，不修改文件**）。

    用于断点指纹主判据（架构设计 12.A.3）。**只读**打开文件分块读取，
    避免大文件爆内存；文件不可读时返回空串（调用方回退路径比对 + 记 WARN）。

    Args:
        excel_path: Excel 文件路径（可为 UNC）。

    Returns:
        形如 ``sha256:1a2b3c...`` 的哈希；读取失败返回 ``""``。
    """
    digest = file_sha256(excel_path)
    if not digest:
        return ""
    return f"{EXCEL_HASH_PREFIX}{digest}"


def build_resume_path(process_dir: str | os.PathLike[str], ticket_no: str) -> Path:
    """构造断点文件路径 ``{过程产出}/断点/resume_{票号}.json``。

    按票号分文件，避免 A 票跑完覆盖 B 票断点（架构设计 12.A.3）。

    Args:
        process_dir: 过程产出目录。
        ticket_no: 票号。

    Returns:
        断点文件 :class:`pathlib.Path`。
    """
    base = normalize_path(process_dir) / "断点"
    safe_ticket = "".join(
        ch for ch in (ticket_no or "").strip() if ch not in '\\/:*?"<>|'
    ) or "UNKNOWN"
    return base / f"resume_{safe_ticket}.json"


class ResumeStore:
    """断点与累积存储（架构设计 12.A.4 ``ResumeStore``）。

    Args:
        path: 断点文件路径（``过程产出/断点/resume_{票号}.json``）。
        fingerprint: 本次数据源指纹（写入断点，用于下次匹配）。
        allowed_root: **允许写入的根目录**（过程产出）；给定则写入前断言归属，
            越界抛 :class:`infra.errors.OutputPathViolation`。缺省不校验（供测试）。

    Attributes:
        path: 断点文件路径。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        fingerprint: Fingerprint | None = None,
        *,
        allowed_root: str | os.PathLike[str] | None = None,
        autosave: bool = True,
    ) -> None:
        """构造断点存储。

        Args:
            path: 断点文件路径。
            fingerprint: 数据源指纹；``None`` 时用空指纹（不参与匹配）。
            allowed_root: 允许写入的根目录（产物分流护栏）；``None`` 表示不校验。
            autosave: 每次 ``mark_done`` / ``accumulate`` 是否立即落盘
                （R3 要求"逐条落盘"，默认 ``True``）。
        """
        self.path: Path = normalize_path(path)
        self.fingerprint: Fingerprint = (
            fingerprint if fingerprint is not None else Fingerprint()
        )
        self._allowed_root: Path | None = (
            normalize_path(allowed_root) if allowed_root is not None else None
        )
        self._autosave: bool = autosave

        self._done_keys: set[str] = set()
        self._accumulated: dict[str, CheckResult] = {}
        self._updated_at: str = ""
        self._log = get_logger(Phase.PHASE3)

        self._assert_within_allowed()

    # ══════════════════════════════════════════════════════════
    #  公共接口
    # ══════════════════════════════════════════════════════════

    def load_if_match(self) -> ResumeSnapshot | None:
        """加载断点——**仅当指纹匹配时返回**，否则 ``None``。

        匹配规则（架构设计 12.A.3，全部满足才算匹配；由
        :meth:`core.models.Fingerprint.is_same_source` 实现）：

          * ``ticket_no`` 严格相等；
          * ``excel_hash`` 严格相等（双方均非空时必查；为空回退路径比对 + 记 WARN）；
          * ``share_root`` 规范化后相等；
          * ``variant`` 相等。

        Returns:
            匹配的 :class:`core.models.ResumeSnapshot`；不匹配 / 文件不存在 / 损坏 → ``None``。
        """
        if not self.path.is_file():
            return None

        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self._log.warning(f"断点文件不可读或损坏，已忽略：{self.path}（{exc}）")
            return None

        if not isinstance(payload, dict):
            self._log.warning(f"断点文件结构非法（非字典），已忽略：{self.path}")
            return None

        stored_fp = Fingerprint.from_dict(payload.get("fingerprint") or {})
        stored_fp = self._with_stored_hash(stored_fp)

        if not self._matches(stored_fp):
            self._log.info(
                f"发现 {stored_fp.ticket_no or '-'} 的断点，与当前数据源不匹配，已忽略"
            )
            return None

        raw_keys = payload.get("done_keys") or []
        done_keys: set[str] = set()
        if isinstance(raw_keys, (list, tuple)):
            done_keys = {str(k) for k in raw_keys if str(k).strip()}

        snapshot = ResumeSnapshot(
            fingerprint=stored_fp,
            done_keys=done_keys,
            updated_at=str(payload.get("updated_at", "") or ""),
        )
        self._done_keys = set(done_keys)
        self._updated_at = snapshot.updated_at
        self._log.info(f"断点匹配成功：已完成 {len(done_keys)} 条，可续跑")
        return snapshot

    def done_keys(self) -> set[str]:
        """返回已完成的 key 集合（副本，外部修改不影响内部状态）。"""
        return set(self._done_keys)

    def is_done(self, key: str) -> bool:
        """判断某 key 是否已完成（续跑跳过依据）。

        Args:
            key: 三级索引唯一键。

        Returns:
            ``True`` 表示该 key 已完成、可跳过。
        """
        return (key or "") in self._done_keys

    def mark_done(self, key: str, result: CheckResult | None = None) -> None:
        """标记某 key 已完成（**幂等**：重复调用以最后一次为准）。

        同时把 ``result`` 累积进内存（供导出时聚合），并（``autosave`` 时）落盘。

        Args:
            key: 三级索引唯一键。
            result: 该 key 的校验结果；``None`` 表示仅记完成（不累积）。
        """
        normalized = (key or "").strip()
        if not normalized:
            return
        self._done_keys.add(normalized)
        if result is not None:
            self._accumulated[normalized] = result
        self._touch()
        if self._autosave:
            self.flush()

    def accumulate(self, key: str, result: CheckResult) -> None:
        """累积某 key 的校验结果（**幂等**：同 key 覆盖，以最后一次为准）。

        Args:
            key: 三级索引唯一键。
            result: 校验结果。
        """
        normalized = (key or "").strip()
        if not normalized or result is None:
            return
        self._accumulated[normalized] = result
        self._touch()
        if self._autosave:
            self.flush()

    def accumulated_results(self) -> list[CheckResult]:
        """返回累积的校验结果列表（按 key 升序，供导出）。

        Returns:
            :class:`core.models.CheckResult` 列表。
        """
        return [self._accumulated[k] for k in sorted(self._accumulated)]

    def done_count(self) -> int:
        """已完成条数。"""
        return len(self._done_keys)

    def flush(self) -> Path:
        """把断点落盘（v2 复合结构）。

        Raises:
            OutputPathViolation: 写入目标越界（产物分流违规）。
            OSError: 写盘失败（**不吞异常**，调用方决定如何提示）。
        """
        self._assert_within_allowed()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        fingerprint_dict = self.fingerprint.to_dict()
        fingerprint_dict["done_count"] = len(self._done_keys)

        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "fingerprint": fingerprint_dict,
            "done_keys": sorted(self._done_keys),
            "updated_at": self._updated_at or _now_iso(),
        }
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._log.error(f"断点写入失败：{self.path}（{exc}）")
            raise
        return self.path

    def reset(self) -> None:
        """清空断点（「重新开始」时调用）：删除断点文件 + 清空内存状态。

        Raises:
            OSError: 删除文件失败（文件被占用等）。
        """
        self._done_keys.clear()
        self._accumulated.clear()
        self._updated_at = ""
        if self.path.is_file():
            self.path.unlink()
        self._log.info(f"断点已重置（重新开始）：{self.path.name}")

    def clear_accumulated(self) -> None:
        """仅清空累积结果（不动断点标记），供「复核后重新导出」使用。"""
        self._accumulated.clear()

    # ══════════════════════════════════════════════════════════
    #  内部实现
    # ══════════════════════════════════════════════════════════

    def _matches(self, stored: Fingerprint) -> bool:
        """判断存储的指纹是否与当前数据源指纹匹配。"""
        current = self.fingerprint

        # ticket_no / share_root / variant 必须严格（规范化后）相等
        if (stored.ticket_no or "") != (current.ticket_no or ""):
            return False
        if _norm_root(stored.share_root) != _norm_root(current.share_root):
            return False
        if (stored.variant or "") != (current.variant or ""):
            return False

        # excel_hash 为主判据；双方均非空时必查
        if stored.excel_hash and current.excel_hash:
            return stored.excel_hash == current.excel_hash

        # 回退：路径规范化比对（记 WARN）
        self._log.warning(
            "断点指纹 excel_hash 缺失或不可读，已回退为 Excel 路径规范化比对（弱判据）"
        )
        return _norm_root(stored.excel_path) == _norm_root(current.excel_path)

    def _with_stored_hash(self, stored: Fingerprint) -> Fingerprint:
        """若存储指纹无 hash 但当前可取（同路径文件可读），补上当前 hash 供比对。

        仅当双方路径一致时补写，避免把"不同文件"误当同源。

        Args:
            stored: 从文件读出的指纹。

        Returns:
            处理后的指纹（必要时替换为空——保证回退路径生效）。
        """
        if stored.excel_hash:
            return stored
        current = self.fingerprint
        if _norm_root(stored.excel_path) != _norm_root(current.excel_path):
            return stored
        recomputed = compute_excel_hash(stored.excel_path) if stored.excel_path else ""
        if not recomputed:
            return stored
        return Fingerprint(
            ticket_no=stored.ticket_no,
            excel_path=stored.excel_path,
            excel_hash=recomputed,
            share_root=stored.share_root,
            variant=stored.variant,
            total_count=stored.total_count,
        )

    def _assert_within_allowed(self) -> None:
        """断言写入目标落在允许根内（产物分流铁律）。

        Raises:
            OutputPathViolation: 目标越界。
        """
        if self._allowed_root is None:
            return
        assert_within(self.path, self._allowed_root)

    def _touch(self) -> None:
        """更新 ``updated_at`` 时间戳。"""
        self._updated_at = _now_iso()


def _now_iso() -> str:
    """返回当前时间的 ISO 8601 字符串（秒级）。"""
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _norm_root(value: str) -> str:
    """规范化路径用于比对（统一分隔符/去尾斜杠/盘符小写）。"""
    text = ("" if value is None else str(value)).strip()
    if not text:
        return ""
    return normalize_path_str(text)
