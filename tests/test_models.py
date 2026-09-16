"""数据模型与 key 构造的回归测试（T01）。"""

from __future__ import annotations

from core.models import (
    CheckResult,
    DeclarationRecord,
    Fingerprint,
    ImageEvidence,
    NoiseLevel,
    OcrText,
    ResumeSnapshot,
    StructureVariant,
    Verdict,
    build_key,
    split_key,
)


class TestBuildKey:
    """三级索引 key 构造（架构设计 9.2）。"""

    def test_basic_format(self) -> None:
        """顺序固定、分隔符固定为半角竖线。"""
        key = build_key("SA26090215", "N011901-007386-001", "2660326M")
        assert key == "SA26090215|N011901-007386-001|2660326M"

    def test_empty_segments_preserved(self) -> None:
        """空段保留（不省略分隔符），段数恒为 3。"""
        key = build_key("SA1", "", "ORD")
        assert key == "SA1||ORD"
        assert key.count("|") == 2

    def test_strip_but_keep_case(self) -> None:
        """各段 strip 后保留原大小写（不 upper）。"""
        key = build_key("  sa26090215  ", " n01 ", " 2660M ")
        assert key == "sa26090215|n01|2660M"

    def test_none_safe(self) -> None:
        """``None`` 输入安全（视为空段）。"""
        assert build_key("", "", "") == "||"

    def test_split_roundtrip(self) -> None:
        """build_key / split_key 往返一致。"""
        key = build_key("SA1", "PN1", "ORD1")
        assert split_key(key) == ("SA1", "PN1", "ORD1")

    def test_split_pads_missing(self) -> None:
        """段数不足时以空串补齐。"""
        assert split_key("A|B") == ("A", "B", "")
        assert split_key("") == ("", "", "")


class TestEnums:
    """枚举取值（与架构类图严格对齐）。"""

    def test_verdict_values(self) -> None:
        assert {v.value for v in Verdict} == {"PASS", "FAIL", "NO_MARK", "NO_IMAGE"}

    def test_noise_level_values(self) -> None:
        assert {n.value for n in NoiseLevel} == {
            "DEFINITE_MATCH",
            "SUSPICIOUS",
            "CLEAR_MISMATCH",
        }

    def test_structure_variant_values(self) -> None:
        """变体 A–E 与架构文档一致。"""
        values = {s.value for s in StructureVariant}
        assert {"A", "B", "C", "D", "E"}.issubset(values)


class TestDeclarationRecord:
    """申报记录模型。"""

    def test_key_uses_three_segments(self) -> None:
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007386-001",
            order_no="2660326M",
        )
        assert record.key() == "SA26090215|N011901-007386-001|2660326M"

    def test_to_dict_contains_evidences(self) -> None:
        record = DeclarationRecord(ticket_no="T", part_no="P", order_no="O")
        record.evidences.append(ImageEvidence(image_path="x.jpg", seq=1, exists=True))
        data = record.to_dict()
        assert data["ticket_no"] == "T"
        assert len(data["evidences"]) == 1
        assert data["evidences"][0]["seq"] == 1


class TestOcrText:
    """OCR 文本模型。"""

    def test_lines_filters_blank(self) -> None:
        ocr = OcrText(text_raw="a\n\n  b  \n")
        assert ocr.lines() == ["a", "b"]

    def test_to_dict_excludes_boxes(self) -> None:
        """JSON 落盘不含 boxes（体积控制），但保留完整原文。"""
        ocr = OcrText(image_path="a.jpg", text_raw="hello", confidence=0.9)
        data = ocr.to_dict()
        assert data["text_raw"] == "hello"
        assert "boxes" not in data


class TestCheckResultRow:
    """13 列行拼装（顺序须与 ResultExporter.COLUMNS 一致）。"""

    def test_to_row_has_13_columns(self) -> None:
        from core.constants import COLUMN_COUNT, verdict_text

        record = DeclarationRecord(
            ticket_no="SA1",
            part_no="P1",
            order_no="O1",
            decl_brand="baori",
            decl_model="A7A01G",
        )
        result = CheckResult(key=record.key(), record=record, verdict=Verdict.PASS)
        row = result.to_row()
        assert len(row) == COLUMN_COUNT == 13
        assert row[0] == "SA1"
        assert row[8] == verdict_text(Verdict.PASS)

    def test_to_row_without_record_is_safe(self) -> None:
        """无 record 时不崩（导出兜底）。"""
        result = CheckResult(key="||", record=None, verdict=Verdict.NO_IMAGE)
        row = result.to_row()
        assert len(row) == 13
        assert row[0] == ""


class TestFingerprint:
    """断点指纹比对（架构设计 12.A.3，防串票）。"""

    def _fp(self, **kwargs) -> Fingerprint:
        base = dict(
            ticket_no="SA26090215",
            excel_path=r"d:\x\a.xlsx",
            excel_hash="sha256:abc",
            share_root=r"\\host\share\ticket",
            variant="D",
            total_count=18,
        )
        base.update(kwargs)
        return Fingerprint(**base)

    def test_same_source_matches(self) -> None:
        assert self._fp().is_same_source(self._fp()) is True

    def test_different_ticket_no_rejected(self) -> None:
        assert self._fp().is_same_source(self._fp(ticket_no="SA26090084")) is False

    def test_different_excel_hash_rejected(self) -> None:
        assert self._fp().is_same_source(self._fp(excel_hash="sha256:zzz")) is False

    def test_different_share_root_rejected(self) -> None:
        assert self._fp().is_same_source(self._fp(share_root=r"\\host\share\other")) is False

    def test_different_variant_rejected(self) -> None:
        assert self._fp().is_same_source(self._fp(variant="E")) is False

    def test_hash_empty_falls_back_to_path(self) -> None:
        """hash 为空时回退路径比对（架构设计 12.A.3）。"""
        left = self._fp(excel_hash="", excel_path=r"d:\x\a.xlsx")
        right = self._fp(excel_hash="", excel_path=r"d:\x\a.xlsx")
        assert left.is_same_source(right) is True
        assert left.is_same_source(self._fp(excel_hash="", excel_path=r"d:\x\b.xlsx")) is False

    def test_roundtrip_dict(self) -> None:
        fp = self._fp()
        assert Fingerprint.from_dict(fp.to_dict()) == fp


class TestResumeSnapshot:
    """断点快照。"""

    def test_done_count_and_schema(self) -> None:
        snap = ResumeSnapshot(
            fingerprint=Fingerprint(ticket_no="T"),
            done_keys={"a|b|c", "d|e|f"},
            updated_at="2026-09-16T12:15:03",
        )
        assert snap.done_count == 2
        data = snap.to_dict()
        assert data["schema"] == 2
        assert data["done_keys"] == ["a|b|c", "d|e|f"]  # 排序稳定
