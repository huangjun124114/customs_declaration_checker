"""T04 断点存储回归测试（``tests/test_resume_store.py``，架构设计 12.A.5 明确要求新增）。

覆盖：

  * ``resume.json`` **v2 复合结构**（含 ``Fingerprint``）
  * ``load_if_match()``：票号 / excel_hash / share_root **三不匹配均返回 None**
  * ``reset()``：「重新开始」清空断点
  * **幂等**：同 key 重复写以最后一次为准；中断后重跑不回退
  * 按票号分文件（``resume_{票号}.json``）
  * excel_hash 空 → 回退路径比对
  * 写入越界抛 ``OutputPathViolation``
"""

from __future__ import annotations

import json

import pytest

from core.models import CheckResult, DeclarationRecord, Fingerprint, Verdict
from core.resume_store import (
    EXCEL_HASH_PREFIX,
    SCHEMA_VERSION,
    ResumeStore,
    build_resume_path,
    compute_excel_hash,
)
from infra.errors import OutputPathViolation


def make_result(key: str, verdict: Verdict = Verdict.PASS) -> CheckResult:
    """构造一条校验结果。"""
    ticket, part, order = key.split("|")
    record = DeclarationRecord(
        ticket_no=ticket, part_no=part, order_no=order, decl_brand="baori", decl_model="A7A01G"
    )
    return CheckResult(key=key, record=record, verdict=verdict, reason="测试")


def make_fingerprint(**overrides) -> Fingerprint:
    """构造指纹（默认含 hash）。"""
    base = {
        "ticket_no": "SA26090215",
        "excel_path": r"D:\\data\\申报要素-SA26090215.XLSX",
        "excel_hash": "sha256:AAAA1111",
        "share_root": r"\\172.20.99.220\share\SA26090215",
        "variant": "D",
        "total_count": 18,
    }
    base.update(overrides)
    return Fingerprint(**base)


# ══════════════════════════════════════════════════════════════════
#  路径构造
# ══════════════════════════════════════════════════════════════════


class TestResumePath:
    """断点文件按票号分文件。"""

    def test_path_by_ticket(self, tmp_path) -> None:
        path = build_resume_path(tmp_path / "过程产出", "SA26090215")
        assert path.name == "resume_SA26090215.json"
        assert path.parent.name == "断点"

    def test_path_sanitized(self, tmp_path) -> None:
        path = build_resume_path(tmp_path / "过程产出", "SA/2609:0215")
        assert "resume_SA26090215.json" == path.name


# ══════════════════════════════════════════════════════════════════
#  excel_hash 计算
# ══════════════════════════════════════════════════════════════════


class TestExcelHash:
    """sha256 流式计算（只读）。"""

    def test_hash_prefix(self, tmp_path) -> None:
        excel = tmp_path / "test.xlsx"
        excel.write_bytes(b"hello excel content")
        digest = compute_excel_hash(excel)
        assert digest.startswith(EXCEL_HASH_PREFIX)
        assert len(digest) > len(EXCEL_HASH_PREFIX) + 10

    def test_hash_unreadable(self, tmp_path) -> None:
        assert compute_excel_hash(tmp_path / "not-exist.xlsx") == ""

    def test_hash_does_not_modify(self, tmp_path) -> None:
        excel = tmp_path / "test.xlsx"
        excel.write_bytes(b"content")
        before = excel.stat().st_mtime_ns
        compute_excel_hash(excel)
        assert excel.stat().st_mtime_ns == before


# ══════════════════════════════════════════════════════════════════
#  指纹匹配（防串票核心）
# ══════════════════════════════════════════════════════════════════


class TestLoadIfMatch:
    """``load_if_match`` 指纹匹配。"""

    def test_match_returns_snapshot(self, tmp_path) -> None:
        """指纹完全匹配 → 返回快照。"""
        fp = make_fingerprint()
        store = ResumeStore(tmp_path / "resume.json", fp)
        store.mark_done("SA26090215|N011901-007386-001|2660326M")

        store2 = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        snapshot = store2.load_if_match()
        assert snapshot is not None
        assert snapshot.done_count == 1
        assert "SA26090215|N011901-007386-001|2660326M" in snapshot.done_keys

    def test_ticket_mismatch_returns_none(self, tmp_path) -> None:
        """**票号不同 → None**（硬断言）。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint(ticket_no="SA26090215"))
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(tmp_path / "resume.json", make_fingerprint(ticket_no="SA26090216"))
        assert other.load_if_match() is None

    def test_excel_hash_mismatch_returns_none(self, tmp_path) -> None:
        """**excel_hash 不同 → None**（硬断言）。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint(excel_hash="sha256:AAAA"))
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(tmp_path / "resume.json", make_fingerprint(excel_hash="sha256:BBBB"))
        assert other.load_if_match() is None

    def test_share_root_mismatch_returns_none(self, tmp_path) -> None:
        """**share_root 不同 → None**（硬断言）。"""
        store = ResumeStore(
            tmp_path / "resume.json", make_fingerprint(share_root=r"\\172.20.99.220\share\A")
        )
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(
            tmp_path / "resume.json", make_fingerprint(share_root=r"\\172.20.99.220\share\B")
        )
        assert other.load_if_match() is None

    def test_variant_mismatch_returns_none(self, tmp_path) -> None:
        """variant 不同 → None（结构变了断点不可信）。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint(variant="D"))
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(tmp_path / "resume.json", make_fingerprint(variant="B"))
        assert other.load_if_match() is None

    def test_missing_file_returns_none(self, tmp_path) -> None:
        """断点文件不存在 → None。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        assert store.load_if_match() is None

    def test_corrupt_file_returns_none(self, tmp_path) -> None:
        """断点文件损坏 → None（不抛）。"""
        path = tmp_path / "resume.json"
        path.write_text("{ not valid json", encoding="utf-8")
        store = ResumeStore(path, make_fingerprint())
        assert store.load_if_match() is None

    def test_hash_empty_fallback_to_path(self, tmp_path) -> None:
        """excel_hash 双方为空 → 回退路径比对（记 WARN）。"""
        fp = make_fingerprint(excel_hash="")
        store = ResumeStore(tmp_path / "resume.json", fp)
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(tmp_path / "resume.json", make_fingerprint(excel_hash=""))
        snapshot = other.load_if_match()
        assert snapshot is not None  # 路径一致 → 匹配

    def test_share_root_normalized_match(self, tmp_path) -> None:
        """share_root 尾斜杠 / 分隔符差异不影响匹配（规范化）。"""
        store = ResumeStore(
            tmp_path / "resume.json", make_fingerprint(share_root=r"\\172.20.99.220\share\A")
        )
        store.mark_done("SA26090215|P|O")

        other = ResumeStore(
            tmp_path / "resume.json", make_fingerprint(share_root="//172.20.99.220/share/A/")
        )
        assert other.load_if_match() is not None


# ══════════════════════════════════════════════════════════════════
#  v2 结构
# ══════════════════════════════════════════════════════════════════


class TestSchemaV2:
    """``resume.json`` v2 复合结构。"""

    def test_schema_version(self, tmp_path) -> None:
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        store.mark_done("SA26090215|P|O")
        payload = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
        assert payload["schema"] == SCHEMA_VERSION == 2
        assert "fingerprint" in payload
        assert "done_keys" in payload
        assert "updated_at" in payload

    def test_fingerprint_fields(self, tmp_path) -> None:
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        store.mark_done("SA26090215|P|O")
        payload = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
        fp = payload["fingerprint"]
        for key in ("ticket_no", "excel_path", "excel_hash", "share_root", "variant", "total_count"):
            assert key in fp
        assert fp["done_count"] == 1

    def test_utf8_no_escape(self, tmp_path) -> None:
        """中文路径不被转义（ensure_ascii=False）。"""
        store = ResumeStore(
            tmp_path / "resume.json", make_fingerprint(excel_path=r"D:\数据\申报要素.xlsx")
        )
        store.mark_done("SA26090215|P|O")
        raw = (tmp_path / "resume.json").read_text(encoding="utf-8")
        assert "申报要素" in raw


# ══════════════════════════════════════════════════════════════════
#  幂等 / 不回退
# ══════════════════════════════════════════════════════════════════


class TestIdempotency:
    """幂等：同 key 重复写以最后一次为准；中断重跑不回退。"""

    def test_same_key_twice_last_wins(self, tmp_path) -> None:
        """同 key 写两次 → 只记一次，结果以最后一次为准。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        key = "SA26090215|N011901-007386-001|2660326M"
        store.mark_done(key, make_result(key, Verdict.NO_MARK))
        store.mark_done(key, make_result(key, Verdict.PASS))

        assert store.done_count() == 1
        accumulated = store.accumulated_results()
        assert len(accumulated) == 1
        assert accumulated[0].verdict == Verdict.PASS

    def test_accumulate_idempotent(self, tmp_path) -> None:
        """``accumulate`` 同 key 覆盖。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        key = "SA26090215|P|O"
        store.accumulate(key, make_result(key, Verdict.NO_IMAGE))
        store.accumulate(key, make_result(key, Verdict.PASS))
        assert len(store.accumulated_results()) == 1
        assert store.accumulated_results()[0].verdict == Verdict.PASS

    def test_no_regression_on_resume(self, tmp_path) -> None:
        """中断后重跑：已完成 key 不回退（只增不减）。"""
        path = tmp_path / "resume.json"
        store = ResumeStore(path, make_fingerprint())
        for i in range(5):
            store.mark_done(f"SA26090215|P|O{i}")
        assert store.done_count() == 5

        # 模拟中断 → 重启 → 加载断点 → 继续
        store2 = ResumeStore(path, make_fingerprint())
        snapshot = store2.load_if_match()
        assert snapshot is not None
        assert snapshot.done_count == 5
        store2.mark_done("SA26090215|P|O5")
        assert store2.done_count() == 6
        assert {"SA26090215|P|O0", "SA26090215|P|O4"}.issubset(store2.done_keys())

    def test_is_done(self, tmp_path) -> None:
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        key = "SA26090215|P|O"
        assert not store.is_done(key)
        store.mark_done(key)
        assert store.is_done(key)

    def test_empty_key_ignored(self, tmp_path) -> None:
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        store.mark_done("")
        store.mark_done("   ")
        assert store.done_count() == 0


# ══════════════════════════════════════════════════════════════════
#  reset（重新开始）
# ══════════════════════════════════════════════════════════════════


class TestReset:
    """``reset()``：清空断点。"""

    def test_reset_clears_file(self, tmp_path) -> None:
        path = tmp_path / "resume.json"
        store = ResumeStore(path, make_fingerprint())
        store.mark_done("SA26090215|P|O")
        assert path.is_file()

        store.reset()
        assert not path.is_file()
        assert store.done_count() == 0

    def test_reset_then_fresh(self, tmp_path) -> None:
        """reset 后 load_if_match 返回 None（重新开始）。"""
        path = tmp_path / "resume.json"
        store = ResumeStore(path, make_fingerprint())
        store.mark_done("SA26090215|P|O")
        store.reset()

        store2 = ResumeStore(path, make_fingerprint())
        assert store2.load_if_match() is None


# ══════════════════════════════════════════════════════════════════
#  产物分流护栏
# ══════════════════════════════════════════════════════════════════


class TestPathGuard:
    """断点必须落在过程产出根内。"""

    def test_within_allowed(self, tmp_path) -> None:
        process = tmp_path / "过程产出"
        process.mkdir()
        store = ResumeStore(
            process / "断点" / "resume_SA.json",
            make_fingerprint(),
            allowed_root=process,
        )
        store.mark_done("SA26090215|P|O")
        assert (process / "断点" / "resume_SA.json").is_file()

    def test_out_of_allowed_raises(self, tmp_path) -> None:
        """越界写入 → ``OutputPathViolation``。"""
        process = tmp_path / "过程产出"
        process.mkdir()
        elsewhere = tmp_path / "其它" / "resume_SA.json"

        with pytest.raises(OutputPathViolation):
            ResumeStore(elsewhere, make_fingerprint(), allowed_root=process)

    def test_done_keys_is_copy(self, tmp_path) -> None:
        """``done_keys()`` 返回副本，外部修改不影响内部。"""
        store = ResumeStore(tmp_path / "resume.json", make_fingerprint())
        store.mark_done("SA26090215|P|O")
        keys = store.done_keys()
        keys.add("hacked|X|Y")
        assert store.done_count() == 1
