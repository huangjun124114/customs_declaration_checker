"""缓存次级指纹回归测试（v0.2.0 批次 3-A · P2 修复）。

QA 发现：``engine.version`` 读不到时降级 ``"unknown"``，而 ``"unknown" == "unknown"``
会**命中** → 「打包态（无 dist-info）→ 引擎换版 → 静默复用旧结果」路径存在。
修复：新增 ``engine_fingerprint``（引擎包内文件 ``(相对路径, size, mtime_ns)`` 摘要），
并以**保守**语义参与命中判定（读不到 → 一律 MISS）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import ocr_cache
from core.models import OcrText
from core.ocr_cache import CACHE_SCHEMA_VERSION, HASH_ALGO, OcrCache, detect_engine_fingerprint

_KEY = "a" * 64


def _write_raw(cache: OcrCache, key: str, payload: dict) -> None:
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
#  一、指纹存在与落盘
# ══════════════════════════════════════════════════════════════════


class TestFingerprintPresence:
    def test_fingerprint_nonempty_when_package_present(self) -> None:
        pytest.importorskip("rapidocr")
        assert detect_engine_fingerprint("rapidocr")

    def test_fingerprint_absent_package_empty(self) -> None:
        """包不存在 → 空串（不抛）。"""
        assert detect_engine_fingerprint("definitely-not-a-real-package-xyz") == ""

    def test_payload_carries_fingerprint(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        cache.put(_KEY, OcrText(text_raw="t", confidence=0.9))
        payload = json.loads(cache.path_for(_KEY).read_text(encoding="utf-8"))
        assert "engine_fingerprint" in payload
        assert payload["engine_fingerprint"] == cache.engine_fingerprint

    def test_identity_includes_fingerprint(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        assert "fingerprint" in cache.identity()["engine"]


# ══════════════════════════════════════════════════════════════════
#  二、命中判定：任一不符 / 不可读 → MISS
# ══════════════════════════════════════════════════════════════════


class TestFingerprintHitRule:
    def test_fingerprint_mismatch_misses(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        cache.put(_KEY, OcrText(text_raw="t", confidence=0.9))
        payload = json.loads(cache.path_for(_KEY).read_text(encoding="utf-8"))
        payload["engine_fingerprint"] = "deadbeef" * 8
        _write_raw(cache, _KEY, payload)
        assert cache.get(_KEY) is None

    def test_fingerprint_match_hits(self, tmp_path: Path) -> None:
        cache = OcrCache(tmp_path)
        cache.put(_KEY, OcrText(text_raw="t", confidence=0.9))
        assert cache.get(_KEY) is not None

    def test_unreadable_fingerprint_conservative_miss(self, tmp_path: Path) -> None:
        """本机指纹读不到（空串）→ 一律 MISS，绝不让 unknown==unknown 命中。"""
        cache = OcrCache(tmp_path, engine_fingerprint="")
        cache.put(_KEY, OcrText(text_raw="t", confidence=0.9))
        assert cache.get(_KEY) is None

    def test_old_payload_without_fingerprint_misses(self, tmp_path: Path) -> None:
        """旧缓存（批次 2，无该字段）→ MISS（保守失效）。"""
        cache = OcrCache(tmp_path, engine_version="3.9.2")
        _write_raw(
            cache,
            _KEY,
            {
                "schema": CACHE_SCHEMA_VERSION,
                "hash_algo": HASH_ALGO,
                "image_hash": _KEY,
                "engine": {"name": cache.engine_name, "version": cache.engine_version},
                "params_fingerprint": cache.params_fingerprint(),
                "lines": [{"text": "t", "score": 0.9, "box": None}],
            },
        )
        assert cache.get(_KEY) is None

    def test_version_unknown_but_fingerprint_saves_us(self, tmp_path: Path) -> None:
        """打包态 version 均为 ``unknown`` 时，靠指纹区分新旧结果。"""
        old = OcrCache(tmp_path, engine_version="unknown", engine_fingerprint="aaaa")
        old.put(_KEY, OcrText(text_raw="t", confidence=0.9))
        # 引擎换版 → 指纹变化 → MISS（而非因 unknown==unknown 命中）
        assert OcrCache(tmp_path, engine_version="unknown", engine_fingerprint="bbbb").get(_KEY) is None
        assert OcrCache(tmp_path, engine_version="unknown", engine_fingerprint="aaaa").get(_KEY) is not None


# ══════════════════════════════════════════════════════════════════
#  三、指纹确实随包内文件变化
# ══════════════════════════════════════════════════════════════════


class TestFingerprintReflectsFiles:
    def test_fingerprint_changes_with_package_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = tmp_path / "fakepkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("x = 1", encoding="utf-8")

        monkeypatch.setattr(ocr_cache, "_package_dir", lambda _name: str(pkg))
        ocr_cache._FINGERPRINT_CACHE.pop("fakepkg", None)  # noqa: SLF001

        first = detect_engine_fingerprint("fakepkg")
        assert first

        # 改动文件内容 → 指纹变化
        (pkg / "mod.py").write_text("x = 2" * 100, encoding="utf-8")
        ocr_cache._FINGERPRINT_CACHE.pop("fakepkg", None)  # noqa: SLF001
        second = detect_engine_fingerprint("fakepkg")
        assert second and second != first

        # 新增文件 → 指纹再变
        (pkg / "new.py").write_text("y = 1", encoding="utf-8")
        ocr_cache._FINGERPRINT_CACHE.pop("fakepkg", None)  # noqa: SLF001
        third = detect_engine_fingerprint("fakepkg")
        assert third not in (first, second)

    def test_fingerprint_missing_dir_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ocr_cache, "_package_dir", lambda _name: str(tmp_path / "nope"))
        ocr_cache._FINGERPRINT_CACHE.pop("missingpkg", None)  # noqa: SLF001
        assert detect_engine_fingerprint("missingpkg") == ""
