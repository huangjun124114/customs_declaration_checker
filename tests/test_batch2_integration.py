"""批次 2 集成测试（v0.2.0 点 5 / 点 7 / 点 8 端到端）。

用**离线 mock OCR 后端**（真实 OCR 调用计数）+ **真实 ImageResolver**（走倒排索引）
+ 合成图片目录驱动 ``CheckPipeline``，验证：

  1. **缓存有效性**：同一夹具跑两遍，第二遍真实 OCR 调用 = 0，且结果完全一致；
  2. **KV 落盘**：详细 JSON 日志含 KV，且 KV **不参与判定**（判定结果与关闭 KV 时一致）；
  3. **坏缓存容错**：写坏一份缓存后重跑不崩，且该图被重新识别；
  4. OCR 缓存落在 ``{process_dir}/ocr_cache/``（过程产出）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.image_resolver import ImageResolver
from core.models import DeclarationRecord, ExcelProbeResult, OcrText, StructureVariant
from core.ocr_engine import OcrBackend, OcrEngine
from core.pipeline import CheckPipeline
from core.rule_repository import RuleRepository


class _CountingBackend(OcrBackend):
    """记录真实识别次数的离线 mock 后端。"""

    def __init__(self, text_map: dict[str, str] | None = None) -> None:
        self.text_map = text_map or {}
        self.calls: list[str] = []

    def recognize(self, image_path) -> OcrText:
        path_str = str(image_path)
        self.calls.append(path_str)
        text = self.text_map.get(path_str) or self.text_map.get(Path(path_str).name, "")
        return OcrText(
            image_path=path_str,
            text_raw=text,
            confidence=0.95,
            boxes=[(0, 0, 10, 10)],
            seq=0,
        )

    def warmup(self) -> None:
        return None


class _FakeProbe:
    """绕过 Excel 解析的假探查器（返回固定记录，便于用合成图片目录驱动管线）。"""

    def __init__(self, records: list[DeclarationRecord], ticket_no: str = "SA2") -> None:
        self._records = records
        self._ticket_no = ticket_no

    def probe(self, path) -> ExcelProbeResult:  # noqa: ANN001 - 对齐真实签名
        return ExcelProbeResult(
            variant=StructureVariant.A_OLD,
            sheet_name="Sheet1",
            header_row=0,
            ticket_no=self._ticket_no,
            records=[r for r in self._records],
            notes=[],
        )


def _write_png(path: Path, width: int = 8, height: int = 6) -> None:
    """写一个含 IHDR 的占位 PNG（内容含文件名 → 各文件哈希不同）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + path.name.encode("utf-8")
    )
    path.write_bytes(header)


_IMAGE_TEXT = "Brand:Skyworth\nMODEL:HS-8AA\nMFR P/N: HS-8AA"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    """合成：图片目录 + dummy Excel + 成果/过程目录。"""
    share = tmp_path / "SA2"
    directory = share / "2660326M"
    _write_png(directory / "2660326M&N011901-007386-001&001.png")
    _write_png(directory / "2660326M&N011901-007386-001&002.png")

    excel = tmp_path / "decl.xlsx"
    excel.write_bytes(b"dummy-excel-content")

    return {
        "share": share,
        "excel": excel,
        "result": tmp_path / "result",
        "process": tmp_path / "logs",
    }


def _one_record() -> DeclarationRecord:
    """构造与合成图片匹配的一条申报记录。"""
    return DeclarationRecord(
        ticket_no="SA2",
        part_no="N011901-007386-001",
        order_no="2660326M",
        decl_brand="Skyworth",
        decl_model="HS-8AA",
    )


def _run(env: dict[str, Path], backend: _CountingBackend, *, kv: bool = True):  # noqa: ANN202
    """跑一次管线（注入 mock 后端 + 真索引分辨器；KV 可选）。"""
    repo = RuleRepository()
    repo.load_all()
    engine = OcrEngine(backend=backend)
    pipeline = CheckPipeline(
        repo,
        probe=_FakeProbe([_one_record()]),
        resolver=ImageResolver(),
        ocr_engine=engine,
        kv_extractor=None if kv else _NullKv(),
    )
    return pipeline.run(
        excel_path=env["excel"],
        share_root=env["share"],
        result_dir=env["result"],
        process_dir=env["process"],
        ticket_no="SA2",
        allowed_result_root=env["result"],
        allowed_process_root=env["process"],
    )


class _NullKv:
    """关闭 KV 时用的空实现（``extract_from_ocr`` 返回空 KV）。"""

    def extract_from_ocr(self, ocr) -> tuple[dict[str, str], list[str]]:  # noqa: ANN001
        return {}, []


# ══════════════════════════════════════════════════════════════════
#  一、缓存有效性（最高优先级：第二遍 0 次真实 OCR，结果一致）
# ══════════════════════════════════════════════════════════════════


class TestCacheEffectiveness:
    """同夹具跑两遍：第二遍真实 OCR 调用 = 0，结果完全一致。"""

    def test_second_run_no_real_ocr_and_identical(self, env: dict[str, Path]) -> None:
        backend1 = _CountingBackend({"2660326M&N011901-007386-001&001.png": _IMAGE_TEXT,
                                     "2660326M&N011901-007386-001&002.png": _IMAGE_TEXT})
        first = _run(env, backend1)
        assert len(backend1.calls) == 2, "首跑应对两张图各识别一次"

        # 缓存落在过程产出目录
        cache_dir = env["process"] / "ocr_cache"
        assert cache_dir.is_dir()
        assert any(cache_dir.rglob("*.json"))

        # 第二遍：全新引擎（L1 空），应全部命中 L2 磁盘缓存
        backend2 = _CountingBackend()
        second = _run(env, backend2)
        assert backend2.calls == [], "第二遍不应触发任何真实 OCR"

        # 结果完全一致
        assert [r.to_row() for r in first.results] == [r.to_row() for r in second.results]
        assert first.counts == second.counts

    def test_first_run_only_two_misses(self, env: dict[str, Path]) -> None:
        backend = _CountingBackend({"2660326M&N011901-007386-001&001.png": _IMAGE_TEXT,
                                    "2660326M&N011901-007386-001&002.png": _IMAGE_TEXT})
        _run(env, backend)
        assert len(backend.calls) == 2


# ══════════════════════════════════════════════════════════════════
#  二、KV 落盘 + 不参与判定
# ══════════════════════════════════════════════════════════════════


class TestKvIntegration:
    """KV 进详细 JSON；关闭 KV 时判定结果不变。"""

    def _detail_json(self, env: dict[str, Path]) -> dict:
        return json.loads(
            (env["process"] / "校验详细日志_SA2_累积.json").read_text(encoding="utf-8")
        )

    def test_kv_persisted_in_detail_json(self, env: dict[str, Path]) -> None:
        backend = _CountingBackend({"2660326M&N011901-007386-001&001.png": _IMAGE_TEXT,
                                    "2660326M&N011901-007386-001&002.png": _IMAGE_TEXT})
        _run(env, backend)
        payload = self._detail_json(env)
        kv_found = False
        for result in payload["results"]:
            for evidence in result["evidence_images"]:
                ocr = evidence.get("ocr")
                if ocr and ocr.get("kv"):
                    kv_found = True
                    assert "Brand" in ocr["kv"]
        assert kv_found, "详细 JSON 应含提取到的 KV"

    def test_kv_does_not_change_verdict(self, env: dict[str, Path]) -> None:
        """开启 / 关闭 KV，判定结果（13 列）完全一致。"""
        text_map = {"2660326M&N011901-007386-001&001.png": _IMAGE_TEXT,
                    "2660326M&N011901-007386-001&002.png": _IMAGE_TEXT}

        with_kv = _run(env, _CountingBackend(text_map), kv=True)
        # 用独立的过程目录，避免干扰缓存
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env2 = {
                "share": env["share"],
                "excel": env["excel"],
                "result": tmp_path / "result",
                "process": tmp_path / "logs",
            }
            without_kv = _run(env2, _CountingBackend(text_map), kv=False)

        assert [r.to_row() for r in with_kv.results] == [
            r.to_row() for r in without_kv.results
        ]


# ══════════════════════════════════════════════════════════════════
#  三、坏缓存容错
# ══════════════════════════════════════════════════════════════════


class TestBadCacheTolerance:
    """写坏一份缓存 → 重跑不崩，且该图被重新识别。"""

    def test_corrupt_cache_recovers(self, env: dict[str, Path]) -> None:
        text_map = {"2660326M&N011901-007386-001&001.png": _IMAGE_TEXT,
                    "2660326M&N011901-007386-001&002.png": _IMAGE_TEXT}
        _run(env, _CountingBackend(text_map))

        cache_dir = env["process"] / "ocr_cache"
        json_files = list(cache_dir.rglob("*.json"))
        assert len(json_files) >= 2
        # 故意写坏一份（半截 JSON）
        json_files[0].write_text("{ this is not valid json", encoding="utf-8")

        backend2 = _CountingBackend(text_map)
        result = _run(env, backend2)  # 不应抛异常
        assert result.ok is True
        assert len(backend2.calls) >= 1, "损坏缓存应触发重新识别"
