"""管线级并发等价性 + 并发拓扑回归（v0.2.0 批次 3-A · 点 6）。

要点：
  * **并发不改变结果**：同一批图在 ``ocr_workers=2`` 与 ``=8`` 下，
    ``CheckPipeline`` 的 13 列结果与四类计数**完全一致**；
  * **拓扑 = 选项 (a)**：仅抬高**单条记录内**的 worker 数到全局预算（记录间仍串行），
    由 ``_effective_workers`` 体现；本文件用真实图片数驱动它。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core.image_resolver import ImageResolver
from core.models import DeclarationRecord, ExcelProbeResult, OcrText, StructureVariant
from core.ocr_engine import OcrBackend, OcrEngine
from core.pipeline import CheckPipeline
from core.rule_repository import RuleRepository


class _ThreadRecordingBackend(OcrBackend):
    """记录调用线程与次数的确定性后端。"""

    def __init__(self, text_map: dict[str, str] | None = None, *, delay: float = 0.0) -> None:
        self.text_map = text_map or {}
        self.delay = delay
        self.calls: list[str] = []
        self.threads: list[int] = []
        self._lock = threading.Lock()

    def recognize(self, image_path) -> OcrText:  # noqa: ANN001
        path_str = str(image_path)
        with self._lock:
            self.calls.append(path_str)
            self.threads.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        text = self.text_map.get(path_str) or self.text_map.get(Path(path_str).name, "")
        return OcrText(image_path=path_str, text_raw=text, confidence=0.9, boxes=[(0, 0, 1, 1)], seq=0)

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


_TEXT = "Brand:Skyworth\nMODEL:HS-8AA"


def _build_share(tmp_path: Path) -> Path:
    """两条记录：一条 6 张图、一条 3 张图。"""
    share = tmp_path / "SA2"
    for seq in ("001", "002", "003", "004", "005", "006"):
        _write_png(share / "2660326M" / f"2660326M&N011901-007386-001&{seq}.png")
    for seq in ("001", "002", "003"):
        _write_png(share / "2660410M" / f"2660410M&N030999-000003-001&{seq}.png")
    return share


def _records() -> list[DeclarationRecord]:
    return [
        DeclarationRecord(
            ticket_no="SA2",
            part_no="N011901-007386-001",
            order_no="2660326M",
            decl_brand="Skyworth",
            decl_model="HS-8AA",
        ),
        DeclarationRecord(
            ticket_no="SA2",
            part_no="N030999-000003-001",
            order_no="2660410M",
            decl_brand="Skyworth",
            decl_model="HS-8AA",
        ),
    ]


def _run(share: Path, base: Path, workers: int, delay: float = 0.0):  # noqa: ANN202
    """在同一 ``share`` 目录上跑一次管线（``base`` 提供 Excel / 成果 / 过程目录）。"""
    base.mkdir(parents=True, exist_ok=True)
    excel = base / "decl.xlsx"
    excel.write_bytes(b"dummy")
    repo = RuleRepository()
    repo.load_all()
    backend = _ThreadRecordingBackend({}, delay=delay)
    engine = OcrEngine(backend=backend, max_workers=workers)
    pipeline = CheckPipeline(
        repo,
        probe=_FakeProbe(_records()),
        resolver=ImageResolver(),
        ocr_engine=engine,
    )
    result = pipeline.run(
        excel_path=excel,
        share_root=share,
        result_dir=base / "result",
        process_dir=base / "logs",
        ticket_no="SA2",
        allowed_result_root=base / "result",
        allowed_process_root=base / "logs",
    )
    return result, backend


class TestPipelineConcurrencyEquivalence:
    def test_workers_2_and_8_identical(self, tmp_path: Path) -> None:
        # 同一 share（只读）→ 图片路径一致，逐列可比。
        share = _build_share(tmp_path)
        result2, _b2 = _run(share, tmp_path / "w2", workers=2)
        result8, _b8 = _run(share, tmp_path / "w8", workers=8)

        assert [r.to_row() for r in result2.results] == [r.to_row() for r in result8.results]
        assert result2.counts == result8.counts
        assert len(result2.results) == len(result8.results) == 2

    def test_results_grouped_by_record_not_flattened(self, tmp_path: Path) -> None:
        """结果按记录分组，且每条记录覆盖其全部证据图。"""
        share = _build_share(tmp_path)
        result8, backend8 = _run(share, tmp_path / "g8", workers=8)
        assert len(result8.results) == 2
        assert len(backend8.calls) == 9  # 6 + 3 张图各识别一次


class TestEffectiveWorkersTopology:
    def test_effective_workers_uses_global_budget(self, tmp_path: Path) -> None:
        """选项 (a)：单条记录内并发 = ``min(图片数, 全局预算)``。"""
        repo = RuleRepository()
        repo.load_all()
        pipeline = CheckPipeline(repo, ocr_engine=OcrEngine(backend=_ThreadRecordingBackend(), max_workers=8))
        rec = DeclarationRecord(ticket_no="SA2", part_no="P", order_no="O")
        from core.models import ImageEvidence

        rec.evidences = [ImageEvidence(image_path=f"/x/{i}.jpg", exists=True) for i in range(6)]
        assert pipeline._effective_workers(rec) == 6  # noqa: SLF001

        rec.evidences = [ImageEvidence(image_path=f"/x/{i}.jpg", exists=True) for i in range(20)]
        assert pipeline._effective_workers(rec) == 8  # noqa: SLF001  # 受硬上限夹

        rec.evidences = [ImageEvidence(image_path="/x/0.jpg", exists=True)]
        assert pipeline._effective_workers(rec) == 1  # noqa: SLF001

    def test_eight_workers_use_more_threads_than_two(self, tmp_path: Path) -> None:
        """8 worker 的记录确实比 2 worker 用了更多线程（并发被用起来）。"""
        share = _build_share(tmp_path)
        _r2, backend2 = _run(share, tmp_path / "t2", workers=2, delay=0.02)
        _r8, backend8 = _run(share, tmp_path / "t8", workers=8, delay=0.02)
        assert len(set(backend8.threads)) > len(set(backend2.threads))
        assert len(set(backend8.threads)) > 2
