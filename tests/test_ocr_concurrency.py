"""OCR 并发放宽回归测试（v0.2.0 批次 3-A · 点 6 · Q6「放宽到 4–8」）。

覆盖：
  1. 并发上限常量与默认值（``min(8, cpu)`` + ``CUSTOMS_OCR_WORKERS`` 覆盖）；
  2. **R8 换算**：``总线程 ≈ workers × 每引擎 intra_op ≤ 预算``；
  3. **每 worker 独立引擎**（``RapidOcrBackend`` 按线程隔离，风险 R2）；
  4. **⚠️ 并发不改变结果**：``workers=2`` 与 ``workers=8`` 的
     ``text_raw`` / 顺序 / ``confidence`` / ``low_confidence`` **逐字段一致**；
  5. 并发确实被用起来（8 worker 下使用了 > 2 个线程）。
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from core.models import OcrText
from core.ocr_engine import (
    DEFAULT_THREAD_BUDGET,
    WORKERS_ENV_VAR,
    OcrBackend,
    OcrEngine,
    RapidOcrBackend,
    default_intra_op_threads,
    default_ocr_workers,
)

# ══════════════════════════════════════════════════════════════════
#  测试替身
# ══════════════════════════════════════════════════════════════════


class _DelayBackend(OcrBackend):
    """确定性离线后端（可选延时 + 记录调用线程）。"""

    def __init__(
        self,
        text_map: dict[str, str] | None = None,
        *,
        delay: float = 0.0,
        confidence: float = 0.95,
        fail_paths: set[str] | None = None,
    ) -> None:
        self.text_map = text_map or {}
        self.delay = delay
        self.confidence = confidence
        self.fail_paths = set(fail_paths or [])
        self.calls: list[str] = []
        self.threads: list[int] = []
        self._lock = threading.Lock()

    def recognize(self, image_path) -> OcrText:  # noqa: ANN001 - 对齐真实签名
        path_str = str(image_path)
        with self._lock:
            self.calls.append(path_str)
            self.threads.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if path_str in self.fail_paths or Path(path_str).name in self.fail_paths:
            raise RuntimeError(f"mock failure: {path_str}")
        text = self.text_map.get(path_str) or self.text_map.get(Path(path_str).name, "")
        return OcrText(
            image_path=path_str,
            text_raw=text,
            confidence=self.confidence,
            boxes=[(0, 0, 10, 10)],
            seq=0,
        )

    def warmup(self) -> None:
        return None


class _RecordingRapidBackend(RapidOcrBackend):
    """记录每线程引擎构造次数的假后端（不依赖真实 rapidocr）。"""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.built: list[int] = []
        self._build_lock = threading.Lock()

    def _build_engine(self):  # noqa: ANN202
        obj = object()
        with self._build_lock:
            self.built.append(threading.get_ident())
        return obj


def _mk_images(tmp_path: Path, count: int) -> list[Path]:
    """造 ``count`` 个内容互不相同的占位图。"""
    paths: list[Path] = []
    for i in range(count):
        p = tmp_path / f"img{i}.jpg"
        p.write_bytes(f"unique-bytes-{i}".encode())
        paths.append(p)
    return paths


# ══════════════════════════════════════════════════════════════════
#  一、并发上限与默认值
# ══════════════════════════════════════════════════════════════════


class TestWorkersConfig:
    """``MAX_WORKERS_LIMIT`` / ``default_ocr_workers`` / 环境变量覆盖。"""

    def test_max_workers_limit_is_eight(self) -> None:
        """R8 守门常量由 2 放宽到 8（Q6）。"""
        assert OcrEngine.MAX_WORKERS_LIMIT == 8
        assert DEFAULT_THREAD_BUDGET == 8

    def test_default_workers_uses_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 ``CUSTOMS_OCR_WORKERS`` 覆盖默认（现场可回调到 2）。"""
        monkeypatch.setenv(WORKERS_ENV_VAR, "2")
        assert default_ocr_workers() == 2

    def test_default_workers_env_capped_at_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量超过硬上限时被夹到 8。"""
        monkeypatch.setenv(WORKERS_ENV_VAR, "100")
        assert default_ocr_workers() == OcrEngine.MAX_WORKERS_LIMIT

    @pytest.mark.parametrize("bad", ["abc", "0", "-3", "  "])
    def test_default_workers_env_invalid_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """非法环境变量静默退回 CPU 推导（不抛异常）。"""
        monkeypatch.setenv(WORKERS_ENV_VAR, bad)
        assert 1 <= default_ocr_workers() <= DEFAULT_THREAD_BUDGET

    def test_default_workers_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无环境变量时 = ``min(8, cpu_count or 2)``。"""
        import os

        monkeypatch.delenv(WORKERS_ENV_VAR, raising=False)
        assert default_ocr_workers() == max(1, min(8, os.cpu_count() or 2))

    def test_engine_default_workers_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未显式传 ``max_workers`` 时，引擎默认值取自环境变量。"""
        monkeypatch.setenv(WORKERS_ENV_VAR, "3")
        assert OcrEngine().max_workers == 3

    @pytest.mark.parametrize(("requested", "expected"), [(0, 1), (1, 1), (4, 4), (8, 8), (99, 8)])
    def test_engine_clamps_requested(self, requested: int, expected: int) -> None:
        """显式并发数夹在 ``[1, 8]``。"""
        assert OcrEngine(max_workers=requested).max_workers == expected


# ══════════════════════════════════════════════════════════════════
#  二、R8 换算：worker 数 × 每引擎 intra_op ≤ 预算
# ══════════════════════════════════════════════════════════════════


class TestIntraOpBudget:
    """``default_intra_op_threads`` 的换算关系。"""

    @pytest.mark.parametrize(
        ("workers", "expected"), [(1, 2), (2, 2), (3, 2), (4, 2), (5, 1), (6, 1), (8, 1)]
    )
    def test_intra_op_mapping(self, workers: int, expected: int) -> None:
        assert default_intra_op_threads(workers) == expected

    @pytest.mark.parametrize("workers", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_total_threads_within_budget(self, workers: int) -> None:
        """总线程 ``workers × intra_op`` 恒 ≤ 预算（R8 缓解）。"""
        assert workers * default_intra_op_threads(workers) <= DEFAULT_THREAD_BUDGET

    def test_workers_two_matches_legacy_budget(self) -> None:
        """``workers=2`` 时仍为 ``2×2=4`` —— 与 v0.1.0 行为一致，不引入漂移。"""
        assert 2 * default_intra_op_threads(2) == 4

    def test_engine_derives_intra_op(self) -> None:
        """引擎按并发数推导 intra_op；8 worker → 1。"""
        assert OcrEngine(max_workers=8).intra_op_num_threads == 1
        assert OcrEngine(max_workers=2).intra_op_num_threads == 2

    def test_engine_explicit_intra_op_override(self) -> None:
        """显式指定 intra_op 时不被推导覆盖。"""
        assert OcrEngine(max_workers=8, intra_op_num_threads=2).intra_op_num_threads == 2

    def test_backend_receives_engine_intra_op(self) -> None:
        """引擎惰性构造的后端带上了推导/指定的 intra_op。"""
        engine = OcrEngine(max_workers=8)
        backend = engine.backend
        assert isinstance(backend, RapidOcrBackend)
        assert backend._intra_op_num_threads == 1  # noqa: SLF001 - 断言换算落地

    def test_build_engine_uses_valid_params_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ 回归：线程数必须走 ``EngineConfig.onnxruntime.*`` 且以**关键字** ``params=`` 传。

        历史缺陷：① 键名写成 ``Global.intra_op_num_threads``（rapidocr 3.x 非法键）；
        ② 把 dict 当第一个位置参数（被当成 ``config_path``）→ 两处都会让引擎
        **静默回退无参构造**，intra_op 从未生效（单次推理吃满 ~24 线程）。
        """
        captured: dict[str, object] = {}

        class _FakeRapidOCR:
            def __init__(self, config_path=None, params=None) -> None:  # noqa: ANN001
                captured["config_path"] = config_path
                captured["params"] = params

        fake_mod = types.ModuleType("rapidocr")
        fake_mod.RapidOCR = _FakeRapidOCR  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "rapidocr", fake_mod)

        backend = RapidOcrBackend(intra_op_num_threads=3, inter_op_num_threads=1)
        engine = backend._build_engine()

        assert isinstance(engine, _FakeRapidOCR)
        # ① 不能把 dict 当位置参数（否则被当作 config_path）
        assert captured["config_path"] is None
        # ② 键名必须是有效键；且不得残留历史非法键
        assert captured["params"] == {
            "EngineConfig.onnxruntime.intra_op_num_threads": 3,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        assert "Global.intra_op_num_threads" not in (captured["params"] or {})

    def test_construct_engine_falls_back_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非法键 / 不支持 ``params`` 的版本 → 回退无参构造且**不抛异常**。"""

        class _FlakyRapidOCR:
            def __init__(self, config_path=None, params=None) -> None:  # noqa: ANN001
                if params:
                    raise ValueError("Global.intra_op_num_threads is not a valid key.")
                self.fallback = True

        fake_mod = types.ModuleType("rapidocr")
        fake_mod.RapidOCR = _FlakyRapidOCR  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "rapidocr", fake_mod)

        engine = RapidOcrBackend(intra_op_num_threads=2)._build_engine()
        assert isinstance(engine, _FlakyRapidOCR)
        assert engine.fallback is True


# ══════════════════════════════════════════════════════════════════
#  三、每 worker 独立引擎（R2）
# ══════════════════════════════════════════════════════════════════


class TestPerThreadEngineIsolation:
    """``RapidOcrBackend`` 的引擎按线程隔离。"""

    def test_same_thread_reuses_engine(self) -> None:
        backend = _RecordingRapidBackend()
        assert backend._ensure_engine() is backend._ensure_engine()  # noqa: SLF001
        assert len(backend.built) == 1

    def test_distinct_engine_per_thread(self) -> None:
        """N 个线程各持独立引擎实例（构造 N 次）。"""
        backend = _RecordingRapidBackend()
        n = 6
        barrier = threading.Barrier(n)
        collected: list[tuple[int, object, object]] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait()
            first = backend._ensure_engine()  # noqa: SLF001
            second = backend._ensure_engine()  # noqa: SLF001
            with lock:
                collected.append((i, first, second))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(collected) == n
        for _i, first, second in collected:
            assert first is second, "同一线程应复用同一引擎"
        assert len({id(obj) for _i, obj, _s in collected}) == n, "不同线程应各持独立引擎"
        assert len(backend.built) == n

    def test_injected_engine_shared_across_threads(self) -> None:
        """注入的 mock 引擎（无状态）全线程共享，不隔离。"""
        sentinel = object()
        backend = RapidOcrBackend(engine=sentinel)
        n = 4
        out: list[object] = []
        lock = threading.Lock()

        def worker() -> None:
            got = backend._ensure_engine()  # noqa: SLF001
            with lock:
                out.append(got)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(obj is sentinel for obj in out)

    def test_close_resets_thread_local(self) -> None:
        """``close()`` 后本线程重建引擎（注入引擎不受影响）。"""
        backend = _RecordingRapidBackend()
        first = backend._ensure_engine()  # noqa: SLF001
        backend.close()
        second = backend._ensure_engine()  # noqa: SLF001
        assert first is not second
        assert len(backend.built) == 2


# ══════════════════════════════════════════════════════════════════
#  四、⚠️ 最高优先级：并发不改变结果
# ══════════════════════════════════════════════════════════════════


class TestConcurrencyEquivalence:
    """``workers=2`` 与 ``workers=8`` 输出逐字段一致。"""

    def test_workers_two_and_eight_identical(self, tmp_path: Path) -> None:
        paths = _mk_images(tmp_path, 20)
        text_map = {p.name: f"Brand:Skyworth-{p.stem}\nMODEL:HS-{p.stem}" for p in paths}

        results2 = OcrEngine(backend=_DelayBackend(text_map, delay=0.001), max_workers=2).recognize_batch(
            paths, max_workers=2
        )
        results8 = OcrEngine(backend=_DelayBackend(text_map, delay=0.001), max_workers=8).recognize_batch(
            paths, max_workers=8
        )

        assert [r.image_path for r in results2] == [r.image_path for r in results8]
        assert [r.text_raw for r in results2] == [r.text_raw for r in results8]
        assert [r.confidence for r in results2] == [r.confidence for r in results8]
        assert [r.low_confidence for r in results2] == [r.low_confidence for r in results8]
        # 顺序即输入顺序
        assert [r.image_path for r in results8] == [str(p) for p in paths]

    def test_equivalence_with_failures(self, tmp_path: Path) -> None:
        """含失败图时两档并发仍逐字段一致（失败占位确定）。"""
        paths = _mk_images(tmp_path, 12)
        text_map = {p.name: f"t-{p.stem}" for p in paths}
        fail = {str(paths[3]), str(paths[7])}

        results2 = OcrEngine(
            backend=_DelayBackend(text_map, delay=0.001, fail_paths=fail), max_workers=2
        ).recognize_batch(paths, max_workers=2)
        results8 = OcrEngine(
            backend=_DelayBackend(text_map, delay=0.001, fail_paths=fail), max_workers=8
        ).recognize_batch(paths, max_workers=8)

        assert [r.text_raw for r in results2] == [r.text_raw for r in results8]
        assert [r.low_confidence for r in results2] == [r.low_confidence for r in results8]
        assert results2[3].text_raw == "" and results2[3].low_confidence is True
        assert results8[3].low_confidence is True

    def test_duplicate_paths_preserve_count_and_order(self, tmp_path: Path) -> None:
        """重复路径不折叠：输出与输入等长同序。"""
        a = tmp_path / "dup.jpg"
        a.write_bytes(b"dup-bytes")
        paths = [a, a, a]
        results = OcrEngine(backend=_DelayBackend({a.name: "same"}), max_workers=8).recognize_batch(
            paths, max_workers=8
        )
        assert len(results) == 3
        assert all(r.text_raw == "same" for r in results)


class TestConcurrencyActuallyUsed:
    """证明并发真的被用起来（否则等价性测试可能掩盖退化）。"""

    def test_eight_workers_use_more_than_two_threads(self, tmp_path: Path) -> None:
        paths = _mk_images(tmp_path, 16)
        backend = _DelayBackend({p.name: "t" for p in paths}, delay=0.02)
        engine = OcrEngine(backend=backend, max_workers=8, cache_enabled=False)
        engine.recognize_batch(paths, max_workers=8)
        assert len(set(backend.threads)) > 2, "8 worker 下应使用多个线程"

    def test_batch_default_uses_engine_workers(self, tmp_path: Path) -> None:
        """``recognize_batch`` 不传 ``max_workers`` 时用引擎配置值。"""
        paths = _mk_images(tmp_path, 8)
        backend = _DelayBackend({p.name: "t" for p in paths}, delay=0.02)
        engine = OcrEngine(backend=backend, max_workers=8, cache_enabled=False)
        engine.recognize_batch(paths)  # 不传 → 用引擎 8
        assert len(set(backend.threads)) > 2

    def test_single_path_short_circuits(self, tmp_path: Path) -> None:
        """单张图不分发线程池（直接串行）。"""
        paths = _mk_images(tmp_path, 1)
        backend = _DelayBackend()
        OcrEngine(backend=backend, max_workers=8).recognize_batch(paths)
        assert len(backend.calls) == 1
