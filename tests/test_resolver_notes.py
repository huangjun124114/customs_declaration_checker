"""索引 / 降级链告警落到 ``notes`` 回归测试（v0.2.0 批次 3-A · P1 修复）。

QA 独立验证发现：``ImageResolver.fallback_notes`` **只在 resolver 与其单测里存在，
全仓没有任何生产代码把它写入** ``probe_result.notes`` —— 这是观测性缺口，与历史
缺陷 F（降级静默失效）同源。本文件锁定修复：

  1. 索引路径（``ImageIndex.resolve_with_level``）在 L2/L3/L4 降级命中时留 WARN；
  2. 索引不可用 → 回退实时链时留 WARN；
  3. 实时四级链在 L2/L3/L4 降级命中时留 WARN；
  4. **``CheckPipeline`` 把上述 WARN 汇总进 ``PipelineResult.notes``**（端到端）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.image_index import ImageIndex
from core.image_resolver import ImageResolver
from core.models import DeclarationRecord, ExcelProbeResult, OcrText, StructureVariant
from core.ocr_engine import OcrBackend, OcrEngine
from core.pipeline import CheckPipeline
from core.rule_repository import RuleRepository


def _mk(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"\xff\xd8\xff\xe0fake-" + name.encode("utf-8"))
    return target


@pytest.fixture
def share(tmp_path: Path) -> Path:
    """合成票号目录：L2 模糊目录 + L1 精确目录。"""
    root = tmp_path / "SA2"
    root.mkdir(parents=True, exist_ok=True)
    pic = root / "2660308M图片"  # L2（带「图片」后缀）
    for seq in ("001", "002", "003", "004"):
        _mk(pic, f"2660308M&N011901-007957-001&{seq}.jpg")
    _mk(root / "2660326M", "2660326M&N011901-007386-001&001.jpg")  # L1
    return root


def _record(order: str, part: str) -> DeclarationRecord:
    return DeclarationRecord(ticket_no="SA2", part_no=part, order_no=order)


# ══════════════════════════════════════════════════════════════════
#  一、索引路径（路 A）
# ══════════════════════════════════════════════════════════════════


class TestIndexPathNotes:
    """索引路径在降级命中时记录 WARN。"""

    def test_index_l2_hit_records_note(self, share: Path) -> None:
        resolver = ImageResolver(index_enabled=True)
        evidences = resolver.resolve(_record("2660308M", "N011901-007957-001"), share)
        assert len(evidences) == 4
        assert resolver.take_fallback_notes(), "索引在 L2 降级命中应留 WARN"

    def test_index_l1_hit_no_note(self, share: Path) -> None:
        resolver = ImageResolver(index_enabled=True)
        evidences = resolver.resolve(_record("2660326M", "N011901-007386-001"), share)
        assert len(evidences) == 1
        assert resolver.take_fallback_notes() == [], "L1 精确命中不应留 WARN"

    def test_resolve_with_level_reports_l2(self, share: Path) -> None:
        index = ImageIndex(share)
        evidences, level = index.resolve_with_level("N011901-007957-001", "2660308M")
        assert evidences is not None and len(evidences) == 4
        assert level == "L2"

    def test_resolve_with_level_reports_l1(self, share: Path) -> None:
        index = ImageIndex(share)
        _evidences, level = index.resolve_with_level("N011901-007386-001", "2660326M")
        assert level == "L1"


# ══════════════════════════════════════════════════════════════════
#  二、实时链路径（路 B）
# ══════════════════════════════════════════════════════════════════


class TestLiveChainNotes:
    """实时四级链在降级 / 回退时记录 WARN。"""

    def test_live_l2_hit_records_note(self, share: Path) -> None:
        resolver = ImageResolver(index_enabled=False)
        evidences = resolver.resolve(_record("2660308M", "N011901-007957-001"), share)
        assert len(evidences) == 4
        assert resolver.take_fallback_notes(), "实时链 L2 命中应留 WARN"

    def test_live_l1_hit_no_note(self, share: Path) -> None:
        resolver = ImageResolver(index_enabled=False)
        resolver.resolve(_record("2660326M", "N011901-007386-001"), share)
        assert resolver.take_fallback_notes() == []

    def test_index_unavailable_records_fallback(
        self, share: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.image_index as mod

        class _Boom:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "ImageIndex", _Boom)
        resolver = ImageResolver(index_enabled=True)
        evidences = resolver.resolve(_record("2660326M", "N011901-007386-001"), share)
        assert len(evidences) == 1  # 回退实时链仍命中
        notes = resolver.take_fallback_notes()
        assert notes and any("回退" in n for n in notes)

    def test_take_notes_clears(self, share: Path) -> None:
        resolver = ImageResolver(index_enabled=True)
        resolver.resolve(_record("2660308M", "N011901-007957-001"), share)
        assert resolver.take_fallback_notes()
        assert resolver.take_fallback_notes() == [], "取走后应清空，避免跨跑批重复"


# ══════════════════════════════════════════════════════════════════
#  三、端到端：降级信号进 PipelineResult.notes
# ══════════════════════════════════════════════════════════════════


class _CountingBackend(OcrBackend):
    """离线 mock 后端。"""

    def recognize(self, image_path) -> OcrText:  # noqa: ANN001
        return OcrText(image_path=str(image_path), text_raw="Brand:X\nMODEL:Y", confidence=0.9)

    def warmup(self) -> None:
        return None


class _FakeProbe:
    def __init__(self, records: list[DeclarationRecord]) -> None:
        self._records = records

    def probe(self, path) -> ExcelProbeResult:  # noqa: ANN001
        return ExcelProbeResult(
            variant=StructureVariant.A_OLD,
            sheet_name="Sheet1",
            header_row=0,
            ticket_no="SA2",
            records=list(self._records),
            notes=[],
        )


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (8).to_bytes(4, "big")
        + (6).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + path.name.encode("utf-8")
    )


class TestPipelineNotesPropagation:
    """``CheckPipeline`` 把降级 WARN 汇总进 ``PipelineResult.notes``。"""

    def _run(self, tmp_path: Path, *, resolver: ImageResolver):  # noqa: ANN202
        share = tmp_path / "SA2"
        _write_png(share / "2660308M图片" / "2660308M&N011901-007957-001&001.png")
        excel = tmp_path / "decl.xlsx"
        excel.write_bytes(b"dummy")
        repo = RuleRepository()
        repo.load_all()
        pipeline = CheckPipeline(
            repo,
            probe=_FakeProbe([_record("2660308M", "N011901-007957-001")]),
            resolver=resolver,
            ocr_engine=OcrEngine(backend=_CountingBackend()),
        )
        return pipeline.run(
            excel_path=excel,
            share_root=share,
            result_dir=tmp_path / "result",
            process_dir=tmp_path / "logs",
            ticket_no="SA2",
            allowed_result_root=tmp_path / "result",
            allowed_process_root=tmp_path / "logs",
        )

    def test_degraded_resolution_note_in_result(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, resolver=ImageResolver(index_enabled=True))
        assert result.notes, "发生索引降级时 PipelineResult.notes 必须可见"
        assert any("降级" in n for n in result.notes), result.notes

    def test_live_chain_note_in_result(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, resolver=ImageResolver(index_enabled=False))
        assert any("降级" in n for n in result.notes), result.notes
