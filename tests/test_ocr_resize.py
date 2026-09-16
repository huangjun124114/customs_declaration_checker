"""图像降采样（可选参数）回归测试（v0.2.0 批次 3-A · 点 6 · Q5「先对照再定值」）。

覆盖：
  1. ``resize_long_side`` **默认 0 = 关闭**，关闭时**逐字节**走原路径字符串通道；
  2. 开启后按 ``INTER_AREA`` 等比缩小到长边上限（不放大小图）；
  3. ``OcrEngine`` 暴露该参数并派发到后端；
  4. 该参数**进入** :class:`core.ocr_cache.OcrCache` 的 ``params_fingerprint``
     （硬约束：任一不符 → MISS，防新旧缓存混用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import OcrText
from core.ocr_cache import OcrCache
from core.ocr_engine import OcrEngine, RapidOcrBackend


class _Result:
    """模拟 rapidocr 的 ``RapidOCRResult``（仅需 txts/scores/boxes）。"""

    def __init__(self) -> None:
        self.txts = ["X"]
        self.scores = [0.9]
        self.boxes = [[[0, 0], [1, 0], [1, 1], [0, 1]]]


class _CapturingEngine:
    """记录被调用的入参（路径字符串 或 ndarray），返回固定结果。"""

    def __init__(self) -> None:
        self.inputs: list[object] = []

    def __call__(self, content):  # noqa: ANN001, ANN204
        self.inputs.append(content)
        return _Result()


def _write_png(path: Path, width: int, height: int) -> None:
    """用 cv2 编码一个 ``width×height`` 单色 PNG（中文/非中文路径均可）。"""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = (7, 8, 9)
    ok, buf = cv2.imencode(".png", img)
    assert ok is True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


# ══════════════════════════════════════════════════════════════════
#  一、默认关闭：走路径字符串通道（baseline 零漂移）
# ══════════════════════════════════════════════════════════════════


class TestResizeDisabledByDefault:
    """``resize_long_side = 0``（默认）时不改变既有识别通道。"""

    def test_default_is_zero(self) -> None:
        assert OcrEngine().resize_long_side == 0
        assert RapidOcrBackend()._resize_long_side == 0  # noqa: SLF001

    def test_zero_passes_path_string(self, tmp_path: Path) -> None:
        """关闭降采样时，引擎收到的是 **路径字符串**（与 v0.1.0 一致）。"""
        img = tmp_path / "a.png"
        _write_png(img, 40, 20)
        capture = _CapturingEngine()
        RapidOcrBackend(resize_long_side=0, engine=capture).recognize(img)
        assert len(capture.inputs) == 1
        assert isinstance(capture.inputs[0], str)
        assert capture.inputs[0].endswith("a.png")


# ══════════════════════════════════════════════════════════════════
#  二、开启后：等比缩放到长边上限
# ══════════════════════════════════════════════════════════════════


class TestResizeEnabled:
    """开启降采样后走 imdecode + INTER_AREA 缩放。"""

    def test_enabled_passes_array(self, tmp_path: Path) -> None:
        img = tmp_path / "b.png"
        _write_png(img, 40, 20)
        capture = _CapturingEngine()
        RapidOcrBackend(resize_long_side=10, engine=capture).recognize(img)
        import numpy as np

        assert isinstance(capture.inputs[0], np.ndarray)

    def test_long_side_reduced_to_limit(self, tmp_path: Path) -> None:
        img = tmp_path / "c.png"
        _write_png(img, 40, 20)  # 长边 40
        capture = _CapturingEngine()
        RapidOcrBackend(resize_long_side=10, engine=capture).recognize(img)
        arr = capture.inputs[0]
        assert max(arr.shape[:2]) == 10

    def test_aspect_ratio_preserved(self, tmp_path: Path) -> None:
        img = tmp_path / "d.png"
        _write_png(img, 40, 20)  # 2:1
        capture = _CapturingEngine()
        RapidOcrBackend(resize_long_side=10, engine=capture).recognize(img)
        height, width = capture.inputs[0].shape[:2]
        assert (width, height) == (10, 5)

    def test_tall_image_reduced(self, tmp_path: Path) -> None:
        img = tmp_path / "e.png"
        _write_png(img, 20, 40)  # 竖版，长边 = 高
        capture = _CapturingEngine()
        RapidOcrBackend(resize_long_side=10, engine=capture).recognize(img)
        height, width = capture.inputs[0].shape[:2]
        assert (width, height) == (5, 10)


# ══════════════════════════════════════════════════════════════════
#  三、downscale_for_ocr 纯函数
# ══════════════════════════════════════════════════════════════════


class TestDownscaleHelper:
    """``RapidOcrBackend.downscale_for_ocr`` 的零拷贝与边界。"""

    def test_none_returns_none(self) -> None:
        assert RapidOcrBackend.downscale_for_ocr(None, 100) is None

    def test_zero_disabled_returns_same_object(self) -> None:
        np = pytest.importorskip("numpy")
        arr = np.zeros((30, 40, 3), dtype=np.uint8)
        assert RapidOcrBackend.downscale_for_ocr(arr, 0) is arr

    def test_smaller_than_limit_returns_same_object(self) -> None:
        np = pytest.importorskip("numpy")
        arr = np.zeros((10, 20, 3), dtype=np.uint8)
        assert RapidOcrBackend.downscale_for_ocr(arr, 100) is arr

    def test_larger_returns_resized(self) -> None:
        np = pytest.importorskip("numpy")
        arr = np.zeros((100, 200, 3), dtype=np.uint8)
        out = RapidOcrBackend.downscale_for_ocr(arr, 50)
        assert out is not arr
        assert max(out.shape[:2]) == 50


# ══════════════════════════════════════════════════════════════════
#  四、引擎派发 + 缓存指纹（硬约束）
# ══════════════════════════════════════════════════════════════════


class TestResizeWiring:
    """参数落到引擎后端 + 缓存指纹。"""

    def test_engine_exposes_resize(self) -> None:
        assert OcrEngine(resize_long_side=1600).resize_long_side == 1600
        assert OcrEngine(resize_long_side=-5).resize_long_side == 0

    def test_engine_backend_receives_resize(self) -> None:
        engine = OcrEngine(resize_long_side=2000)
        assert engine.backend._resize_long_side == 2000  # noqa: SLF001

    def test_params_fingerprint_includes_resize(self, tmp_path: Path) -> None:
        c0 = OcrCache(tmp_path, resize_long_side=0)
        c1600 = OcrCache(tmp_path, resize_long_side=1600)
        assert c0.params_fingerprint()["resize_long_side"] == 0
        assert c1600.params_fingerprint()["resize_long_side"] == 1600

    def test_resize_change_forces_miss(self, tmp_path: Path) -> None:
        """降采样参数变化 → 旧缓存不可复用（MISS）。"""
        key = "a" * 64
        OcrCache(tmp_path, resize_long_side=0).put(key, OcrText(text_raw="t", confidence=0.9))
        assert OcrCache(tmp_path, resize_long_side=1600).get(key) is None
        assert OcrCache(tmp_path, resize_long_side=0).get(key) is not None
