"""``core.ocr_cache`` 回归测试（v0.2.0 批次 2 / 点 5）。

覆盖：
  1. 落盘往返（text_raw / confidence / kv / line_scores 无损；路径写回当前值）；
  2. 命中判定三要素（image_hash / engine / params_fingerprint）；
  3. 严格 schema + ``created_at`` 本地时区偏移（非 ``Z``）；
  4. 读容错（损坏 / 半截 / 字段缺失 / schema 未知 → MISS，不抛）；
  5. **绝不删除既有文件**；``clear()`` 显式删除；
  6. 并发同哈希原子写；
  7. 纯字节头解析图像尺寸（PNG / BMP；未知 → (0,0)）；
  8. ``detect_engine_version`` 降级不抛。
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

import pytest

from core.models import OcrText
from core.ocr_cache import (
    CACHE_SCHEMA_VERSION,
    HASH_ALGO,
    OcrCache,
    detect_engine_version,
    read_image_size,
)


def _png_bytes(width: int = 8, height: int = 6, tag: bytes = b"") -> bytes:
    """构造含 IHDR 的最小 PNG 头（供 read_image_size 解析）。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + tag
    )


def _bmp_bytes(width: int = 5, height: int = 7) -> bytes:
    """构造最小 BMP 头。"""
    return (
        b"BM"
        + (0).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (54).to_bytes(4, "little")
        + (40).to_bytes(4, "little")
        + width.to_bytes(4, "little", signed=True)
        + height.to_bytes(4, "little", signed=True)
    )


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    """过程产出根目录。"""
    return tmp_path / "logs"


def _hash(ch: str) -> str:
    """构造一个 64 位十六进制哈希。"""
    return (ch * 64)[:64]


# ══════════════════════════════════════════════════════════════════
#  一、往返
# ══════════════════════════════════════════════════════════════════


class TestRoundTrip:
    """写→读无损；命中时路径写回当前值。"""

    def test_put_get_roundtrip(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        ocr = OcrText(
            image_path="/a/x.png",
            text_raw="Brand:Skyworth\n型号:HS-8AA",
            confidence=0.9,
            boxes=[(0, 0, 1, 1), (2, 2, 3, 3)],
            line_scores=[0.95, 0.9],
            kv={"Brand": "Skyworth"},
            low_confidence=False,
        )
        cache.put(_hash("a"), ocr, image_path_hint="/a/x.png")

        got = cache.get(_hash("a"), image_path_hint="/b/y.png")
        assert got is not None
        assert got.text_raw == ocr.text_raw
        assert got.confidence == pytest.approx(0.9)
        assert got.kv == {"Brand": "Skyworth"}
        assert got.line_scores == [0.95, 0.9]
        assert got.image_path == "/b/y.png"  # 路径写回当前路径（按内容键控）

    def test_confidence_recomputed_as_min_score(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        ocr = OcrText(text_raw="a\nb", confidence=0.7, line_scores=[0.7, 0.88])
        cache.put(_hash("b"), ocr)
        got = cache.get(_hash("b"))
        assert got is not None
        assert got.confidence == pytest.approx(0.7)

    def test_get_missing_returns_none_and_counts(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        assert cache.get(_hash("c")) is None
        assert cache.stats()["misses"] == 1


# ══════════════════════════════════════════════════════════════════
#  二、命中判定三要素
# ══════════════════════════════════════════════════════════════════


class TestHitConditions:
    """image_hash / engine / params_fingerprint 任一不符即 MISS。"""

    def test_hit_requires_same_hash(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        cache.put(_hash("d"), OcrText(text_raw="t", confidence=0.9))
        assert cache.get(_hash("e")) is None

    def test_miss_on_engine_version_mismatch(self, logs_dir: Path) -> None:
        OcrCache(logs_dir, engine_version="1.0").put(_hash("f"), OcrText(text_raw="t"))
        cache2 = OcrCache(logs_dir, engine_version="2.0")
        assert cache2.get(_hash("f")) is None

    def test_miss_on_engine_name_mismatch(self, logs_dir: Path) -> None:
        OcrCache(logs_dir, engine_name="rapidocr").put(_hash("0"), OcrText(text_raw="t"))
        cache2 = OcrCache(logs_dir, engine_name="other")
        assert cache2.get(_hash("0")) is None

    def test_miss_on_params_fingerprint_mismatch(self, logs_dir: Path) -> None:
        OcrCache(logs_dir, resize_long_side=0).put(_hash("1"), OcrText(text_raw="t"))
        cache2 = OcrCache(logs_dir, resize_long_side=1024)
        assert cache2.get(_hash("1")) is None

    def test_hit_when_identity_matches(self, logs_dir: Path) -> None:
        OcrCache(logs_dir, engine_version="3.4.0").put(_hash("2"), OcrText(text_raw="t"))
        cache2 = OcrCache(logs_dir, engine_version="3.4.0")
        assert cache2.get(_hash("2")) is not None
        assert cache2.stats()["hits"] == 1


# ══════════════════════════════════════════════════════════════════
#  三、严格 schema
# ══════════════════════════════════════════════════════════════════


class TestSchema:
    """方案 §3.5 严格 schema。"""

    def test_payload_has_required_keys(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        cache.put(_hash("3"), OcrText(text_raw="a", confidence=0.5), image_path_hint="")
        payload = json.loads(cache.path_for(_hash("3")).read_text(encoding="utf-8"))
        for key in (
            "schema",
            "hash_algo",
            "image_hash",
            "image_size",
            "image_path_hint",
            "engine",
            "params_fingerprint",
            "created_at",
            "lines",
            "kv",
            "text_raw",
            "low_confidence",
        ):
            assert key in payload, f"缺少 schema 字段 {key}"
        assert payload["schema"] == CACHE_SCHEMA_VERSION
        assert payload["hash_algo"] == HASH_ALGO
        assert set(payload["engine"]) == {"name", "version"}
        assert set(payload["params_fingerprint"]) == {
            "resize_long_side",
            "intra_op_num_threads",
        }

    def test_created_at_has_local_offset_not_z(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        cache.put(_hash("4"), OcrText(text_raw="a", confidence=0.5))
        created = json.loads(cache.path_for(_hash("4")).read_text(encoding="utf-8"))["created_at"]
        assert not created.endswith("Z")
        parsed = _dt.datetime.fromisoformat(created)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() is not None


# ══════════════════════════════════════════════════════════════════
#  四、读容错（绝不抛）
# ══════════════════════════════════════════════════════════════════


class TestFaultTolerance:
    """损坏 / 半截 / 字段缺失 / schema 未知 → MISS + 计数，绝不抛异常。"""

    def _write_raw(self, cache: OcrCache, key: str, text: str) -> Path:
        target = cache.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_corrupt_json(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        self._write_raw(cache, _hash("5"), "{ half-broken")
        assert cache.get(_hash("5")) is None
        assert cache.stats()["corrupt"] == 1

    def test_missing_fields(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        self._write_raw(cache, _hash("6"), json.dumps({"schema": CACHE_SCHEMA_VERSION}))
        assert cache.get(_hash("6")) is None

    def test_unknown_schema(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        self._write_raw(cache, _hash("7"), json.dumps({"schema": 999}))
        assert cache.get(_hash("7")) is None

    def test_non_dict_root(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        self._write_raw(cache, _hash("8"), json.dumps([1, 2, 3]))
        assert cache.get(_hash("8")) is None

    def test_lines_not_list(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "hash_algo": HASH_ALGO,
            "image_hash": _hash("9"),
            "engine": {"name": cache.engine_name, "version": cache.engine_version},
            "params_fingerprint": cache.params_fingerprint(),
            "lines": "not-a-list",
        }
        self._write_raw(cache, _hash("9"), json.dumps(payload))
        assert cache.get(_hash("9")) is None


# ══════════════════════════════════════════════════════════════════
#  五、删除语义
# ══════════════════════════════════════════════════════════════════


class TestDeletion:
    """绝不删除既有文件（除显式 clear()）。"""

    def test_existing_files_not_deleted_on_write(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        cache.put(_hash("a"), OcrText(text_raw="t", confidence=0.9))
        stray = cache.cache_dir / "stray" / "keep.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("{}", encoding="utf-8")

        cache.get(_hash("a"))
        cache.put(_hash("b"), OcrText(text_raw="t2", confidence=0.9))
        assert stray.is_file()

    def test_clear_removes_all(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)
        cache.put(_hash("a"), OcrText(text_raw="t", confidence=0.9))
        cache.put(_hash("b"), OcrText(text_raw="t", confidence=0.9))
        assert cache.size() == 2
        removed = cache.clear()
        assert removed >= 2
        assert cache.size() == 0

    def test_disabled_cache_is_noop(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir, enabled=False)
        cache.put(_hash("a"), OcrText(text_raw="t", confidence=0.9))
        assert cache.get(_hash("a")) is None
        assert cache.size() == 0


# ══════════════════════════════════════════════════════════════════
#  六、并发原子写
# ══════════════════════════════════════════════════════════════════


class TestConcurrency:
    """多线程写同一哈希不应损坏文件。"""

    def test_concurrent_same_hash_writes(self, logs_dir: Path) -> None:
        cache = OcrCache(logs_dir)

        def worker() -> None:
            for _ in range(15):
                cache.put(_hash("a"), OcrText(text_raw="t", confidence=0.9))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cache.get(_hash("a")) is not None


# ══════════════════════════════════════════════════════════════════
#  七、图像尺寸解析 + 版本降级
# ══════════════════════════════════════════════════════════════════


class TestImageSize:
    """纯字节头解析尺寸。"""

    def test_png(self, tmp_path: Path) -> None:
        p = tmp_path / "x.png"
        p.write_bytes(_png_bytes(12, 34))
        assert read_image_size(p) == (12, 34)

    def test_bmp(self, tmp_path: Path) -> None:
        p = tmp_path / "x.bmp"
        p.write_bytes(_bmp_bytes(5, 7))
        assert read_image_size(p) == (5, 7)

    def test_unknown_bytes(self, tmp_path: Path) -> None:
        p = tmp_path / "x.bin"
        p.write_bytes(b"\x00\x01\x02\x03")
        assert read_image_size(p) == (0, 0)

    def test_missing_file(self, tmp_path: Path) -> None:
        assert read_image_size(tmp_path / "nope.png") == (0, 0)

    def test_size_stored_in_payload(self, logs_dir: Path, tmp_path: Path) -> None:
        img = tmp_path / "y.png"
        img.write_bytes(_png_bytes(20, 10))
        cache = OcrCache(logs_dir)
        cache.put(_hash("c"), OcrText(text_raw="t", confidence=0.9), image_path_hint=str(img))
        payload = json.loads(cache.path_for(_hash("c")).read_text(encoding="utf-8"))
        assert payload["image_size"] == [20, 10]


class TestEngineVersion:
    """``detect_engine_version`` 失败降级为 ``"unknown"``（不抛）。"""

    def test_unknown_package(self) -> None:
        assert detect_engine_version("definitely-not-a-real-package-xyz") == "unknown"
