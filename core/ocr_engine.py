"""OCR 引擎封装（core.ocr_engine，对应架构设计第 4 节类图 + 第 13.3/13.4 节）。

本模块提供**三层解耦**的 OCR 能力：

  1. :class:`OcrBackend` —— 抽象基类（接口），只声明 ``recognize`` / ``warmup``；
     **上层（JudgeEngine / Pipeline）只依赖此接口**，不关心底层实现。
     这正是架构设计第 1 节「OCR 引擎 ↔ 上层解耦」的落地：更换封装库只动一个实现类
     （v1.2 已实证：``rapidocr_onnxruntime`` → ``rapidocr 3.x`` 只改了
     :class:`RapidOcrBackend` 的内部实现，抽象基类与调用方全部不动）。

  2. :class:`RapidOcrBackend` —— 真实实现，按 **rapidocr 3.x 新 API** 调用
     （⚠️ 不是 1.x 的 ``result, elapse = engine(path)`` 元组解包）。

  3. :class:`OcrEngine` —— 编排层：批量并发（``ThreadPoolExecutor``，``max_workers≤2``）、
     **按图片哈希缓存**、单张失败容错、低置信度标注。

关键约定（硬约束，来自架构设计 9.7 / 13.4 / R8）：
  * **禁止** ``cv2.imread(path)`` —— 中文 / UNC 路径会**静默失败返回 ``None``**（已实测）。
    统一用 ``np.frombuffer(open(path, 'rb').read(), np.uint8)`` + ``cv2.imdecode(...)``。
  * onnxruntime ``intra_op_num_threads=2``、``inter_op_num_threads=1``，外层
    ``max_workers≤2`` —— 防止 CPU 线程爆炸（R8）。
  * **缓存 key 用图片内容哈希**（非路径），避免同一张图在不同路径下重复 OCR。
  * **只读红线**：图片只以读字节方式访问，**不复制、不移动、不改名**。

分层：``core`` 只能 import ``core`` / ``infra``，**禁止** import PySide6 / PyQt。
"""

from __future__ import annotations

import abc
import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as _dc_replace
from typing import Any

from core.models import OcrText
from core.ocr_cache import OcrCache
from infra.encoding import normalize_path
from infra.logger import Phase, get_logger

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_HASH_CHUNK_SIZE",
    "DEFAULT_CACHE_MAX_ENTRIES",
    "OcrBackend",
    "RapidOcrBackend",
    "OcrEngine",
    "_safe_sequence",
]

#: 低置信度阈值（架构设计 9.7：低于此值的识别结果标 ``low_confidence=True``，
#: ``NoiseGuard`` 优先视为 ``SUSPICIOUS``）。
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5

#: 图片内容哈希的分块大小（1 MiB，与 ``infra.encoding.file_sha256`` 一致）。
DEFAULT_HASH_CHUNK_SIZE: int = 1024 * 1024

#: OCR 结果缓存的最大条目数（LRU 淘汰；防止长跑批内存无界增长）。
DEFAULT_CACHE_MAX_ENTRIES: int = 512


def _compute_image_hash(image_path: str | os.PathLike[str]) -> str:
    """流式计算图片内容的 sha256（缓存 key，**不用路径**）。

    用内容哈希而非路径作缓存 key，是为了让「同一张图出现在不同路径」
    （例如被复制到临时目录、或 UNC 与本地盘映射同一文件）只 OCR 一次。

    Args:
        image_path: 图片路径。

    Returns:
        十六进制 sha256；读取失败返回空字符串（调用方降级为路径 key）。
    """
    p = normalize_path(image_path)
    digest = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(DEFAULT_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _with_path(ocr: OcrText, path_str: str) -> OcrText:
    """返回把 ``image_path`` 改为 ``path_str`` 的副本（命中缓存时用）。

    缓存按**图片内容哈希**键控，同一内容可能来自不同路径；命中后必须把路径改回
    **本次请求路径**，否则上层 ``by_path`` 回填会失配。``image_path`` 相同时直接
    返回原对象（零拷贝）。

    Args:
        ocr: 缓存中的 OCR 结果。
        path_str: 本次请求路径。

    Returns:
        路径正确的 :class:`core.models.OcrText`。
    """
    if ocr.image_path == path_str:
        return ocr
    return _dc_replace(ocr, image_path=path_str)


def _safe_sequence(value: Any) -> list[Any]:
    """把 rapidocr 返回的数组/元组/列表安全转为 Python ``list``。

    ⚠️ 实测陷阱：``res.boxes`` 是 **numpy.ndarray**，直接写 ``value or []``
    会触发 ``ValueError: truth value of an array is ambiguous``。必须显式判
    ``None`` 与长度，绝不能对数组做布尔求值。

    Args:
        value: ``res.txts`` / ``res.scores`` / ``res.boxes`` 等返回值。

    Returns:
        Python ``list``；``None`` / 空 返回 ``[]``。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    # numpy 数组等序列类型：先判长度（不触发布尔求值），再转 list
    try:
        if len(value) == 0:
            return []
        return list(value)
    except TypeError:
        return [value]


# ══════════════════════════════════════════════════════════════════
#  一、抽象基类（接口）
# ══════════════════════════════════════════════════════════════════


class OcrBackend(abc.ABC):
    """OCR 后端抽象基类（架构设计第 4 节 ``OcrBackend``）。

    上层只依赖本接口；测试可用 mock 后端离线跑，**不依赖真实 rapidocr**。

    实现类契约：
      * :meth:`recognize` **只管单张图**，失败可抛任意异常（由 :class:`OcrEngine` 兜底）；
      * :meth:`warmup` **幂等**，可被多次调用（供首启异步预热）。
    """

    @abc.abstractmethod
    def recognize(self, image_path: str | os.PathLike[str]) -> OcrText:
        """识别单张图片，返回 :class:`core.models.OcrText`。

        Args:
            image_path: 图片路径（本地或 UNC）。

        Returns:
            识别结果。

        Raises:
            Exception: 实现相关的任意异常（由 :class:`OcrEngine` 捕获并降级）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def warmup(self) -> None:
        """预热：提前加载模型（架构设计 R2，规避首启首次 OCR 慢）。

        实现须**幂等**：重复调用不应重复加载。
        """
        raise NotImplementedError

    def close(self) -> None:
        """释放后端资源（默认无操作；可选由实现类覆写）。"""
        return None  # noqa: B027 - 故意留作可选钩子（非抽象，避免强制实现）


# ══════════════════════════════════════════════════════════════════
#  二、RapidOCR 实现（v1.2 第 13.3 节新 API）
# ══════════════════════════════════════════════════════════════════


class RapidOcrBackend(OcrBackend):
    """基于 ``rapidocr`` 3.x 的真实后端（架构设计 13.3 已实测）。

    ⚠️ **API 形态必须照此实现**（rapidocr 3.x，与 1.x 完全不同）：

    * 导入：``from rapidocr import RapidOCR``（惰性导出，``dir()`` 看不到但可 import）；
    * 构造：``RapidOCR()``（初始化约 0.5s，模型加载很快）；
    * 调用：``engine(str(image_path))`` —— **传路径字符串**；
    * 取文本：``res.txts`` —— **``RapidOCRResult`` 对象属性**，不是元组解包；
    * 其他属性：``res.boxes`` / ``res.scores``。

    **绝对禁止**写成 ``result, elapse = engine(path)``（那是 rapidocr 1.x 的 API）。

    中文 / UNC 路径：``RapidOCR(str(path))`` 内部自行读图且**中文路径安全**
    （已实测 ``2660308M图片\\…&001.jpg`` 识别成功）。为满足架构设计 9.7 的
    「统一 ``imdecode``」约定与可测试性，本类另提供 :meth:`load_image_array`
    作为**自备图像读取**通道（中文路径实锤断言用），并可用于未来自管预处理。

    Args:
        intra_op_num_threads: onnxruntime 单算子线程数（R8，默认 2）。
        inter_op_num_threads: onnxruntime 算子间线程数（R8，默认 1）。
        engine: 可选的已构造引擎（**仅供测试注入 mock**；``None`` 时惰性构造）。
    """

    def __init__(
        self,
        intra_op_num_threads: int = 2,
        inter_op_num_threads: int = 1,
        *,
        engine: Any | None = None,
    ) -> None:
        self._intra_op_num_threads = max(1, int(intra_op_num_threads))
        self._inter_op_num_threads = max(1, int(inter_op_num_threads))
        self._engine: Any | None = engine
        self._lock = threading.Lock()
        self._log = get_logger(Phase.OCR)

    # ── 引擎构造 ──────────────────────────────────────────────

    def _ensure_engine(self) -> Any:
        """惰性构造 RapidOCR 引擎（线程安全；构造约 0.5s）。

        Returns:
            ``RapidOCR`` 实例（或测试注入的 mock engine）。
        """
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is None:
                from rapidocr import RapidOCR  # 惰性导入：避免 import 期加载模型

                params: dict[str, Any] = {
                    "Global.intra_op_num_threads": self._intra_op_num_threads,
                    "Global.inter_op_num_threads": self._inter_op_num_threads,
                }
                self._engine = self._construct_engine(RapidOCR, params)
            return self._engine

    @staticmethod
    def _construct_engine(engine_cls: Any, params: dict[str, Any]) -> Any:
        """构造引擎，兼容不同 rapidocr 小版本的构造签名。

        优先按 13.3 的 ``RapidOCR()`` 直调（带线程数参数）；若该版本不接受
        参数则回退为无参构造（线程数交由 onnxruntime 默认，功能不受影响）。

        Args:
            engine_cls: ``RapidOCR`` 类。
            params: 线程数参数。

        Returns:
            引擎实例。
        """
        try:
            return engine_cls(params)
        except TypeError:
            return engine_cls()

    # ── 图像读取（中文 / UNC 路径安全）────────────────────────

    @staticmethod
    def load_image_array(image_path: str | os.PathLike[str]) -> Any:
        """用 ``np.frombuffer + cv2.imdecode`` 读取图片（**中文路径安全**）。

        ⚠️ 架构设计 9.7 / 13.4 强制约定：**禁止** ``cv2.imread(path)``
        —— 中文路径会静默失败返回 ``None``（已实测实锤）。本方法用字节流 + 解码，
        对中文 / UNC 路径均有效。

        Args:
            image_path: 图片路径。

        Returns:
            BGR 图像的 ``numpy.ndarray``；路径不存在或解码失败返回 ``None``。

        Raises:
            ImportError: 缺少 numpy / opencv 依赖（属环境问题）。
        """
        import cv2
        import numpy as np

        p = normalize_path(image_path)
        try:
            raw = p.read_bytes()
        except OSError:
            return None
        buf = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)

    # ── 接口实现 ──────────────────────────────────────────────

    def recognize(self, image_path: str | os.PathLike[str]) -> OcrText:
        """识别单张图片（rapidocr 3.x API）。

        Args:
            image_path: 图片路径。

        Returns:
            :class:`core.models.OcrText`；``text_raw`` 为 ``"\\n".join(res.txts)``，
            ``confidence`` 取 ``min(res.scores)``（保守估计，无 scores 时为 0.0）。

        Raises:
            FileNotFoundError: 图片不存在。
            Exception: 引擎调用失败（由 :class:`OcrEngine` 兜底降级）。
        """
        p = normalize_path(image_path)
        if not p.exists():
            raise FileNotFoundError(f"image not found: {p}")

        engine = self._ensure_engine()
        # ⚠️ 传路径字符串；3.x 返回 RapidOCRResult 对象（非元组）
        result = engine(str(p))

        # ⚠️ res.txts / res.scores / res.boxes 可能是 numpy 数组，必须走 _safe_sequence
        #    （直接 `or []` 会因数组布尔求值歧义抛 ValueError）
        txts = _safe_sequence(getattr(result, "txts", None))
        text_lines = [str(t) for t in txts if t is not None]
        scores = _safe_sequence(getattr(result, "scores", None))
        score_values = [float(s) for s in scores if s is not None]
        confidence = min(score_values) if score_values else 0.0
        boxes = _safe_sequence(getattr(result, "boxes", None))

        return OcrText(
            image_path=str(p),
            text_raw="\n".join(text_lines),
            confidence=confidence,
            boxes=boxes,
            seq=0,
            line_scores=score_values,
        )

    def warmup(self) -> None:
        """预热模型（幂等；供首启异步预加载，架构设计 R2）。"""
        try:
            self._ensure_engine()
        except Exception as exc:  # noqa: BLE001 - 预热失败不应崩溃（首次识别会再试）
            self._log.warning(f"OCR 引擎预热失败（将在首次识别时重试）：{exc}")

    def close(self) -> None:
        """释放引擎引用（进程退出 / 测试清理用）。"""
        with self._lock:
            self._engine = None


# ══════════════════════════════════════════════════════════════════
#  三、编排层（批量并发 + 哈希缓存 + 容错）
# ══════════════════════════════════════════════════════════════════


class _LruCache:
    """极简线程安全 LRU 缓存（避免引入第三方依赖）。

    键为图片内容哈希，值为 :class:`core.models.OcrText`。
    """

    def __init__(self, max_entries: int = DEFAULT_CACHE_MAX_ENTRIES) -> None:
        self._max = max(1, int(max_entries))
        self._data: OrderedDict[str, OcrText] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> OcrText | None:
        """取缓存项（命中则提升为最近使用）。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: str, value: OcrText) -> None:
        """写缓存项（超容则淘汰最久未用）。"""
        if not key:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        """清空缓存并重置命中统计。"""
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class OcrEngine:
    """OCR 编排层（架构设计第 4 节 ``OcrEngine``）。

    职责：
      * **批量并发**：``recognize_batch`` 用 ``ThreadPoolExecutor(max_workers≤2)``
        并行同条记录的多张图（R8：限制并发，防 CPU 线程爆炸）；
      * **单张失败不中断**：任一张抛异常 → 该张返回低置信占位并记 WARN（契约要求）；
      * **两级缓存**：L1 进程内 LRU（``_LruCache``）+ L2 磁盘缓存（``core.ocr_cache``）；
        命中缓存直接返回，避免重复 OCR（key = 图片内容 sha256）；
      * **低置信度标注**：``confidence < threshold`` → ``low_confidence=True``。

    Args:
        backend: OCR 后端；``None`` 时惰性构造 :class:`RapidOcrBackend`。
        max_workers: 并发线程数（**会被夹在 1–2**，落实 R8）。
        confidence_threshold: 低置信度阈值（默认 0.5）。
        cache_enabled: 是否启用哈希缓存（默认 ``True``）。
        cache_max_entries: 缓存最大条目数。
        hash_fn: 图片哈希函数（**供测试注入**；默认内容 sha256）。
        disk_cache: L2 磁盘缓存（``core.ocr_cache.OcrCache``）；``None`` 时仅用 L1，
            可后续经 :meth:`attach_disk_cache` 挂载。
    """

    #: 并发上限（R8：``max_workers≤2``）
    MAX_WORKERS_LIMIT: int = 2

    def __init__(
        self,
        backend: OcrBackend | None = None,
        max_workers: int = 2,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        *,
        cache_enabled: bool = True,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        hash_fn: Callable[[str | os.PathLike[str]], str] | None = None,
        disk_cache: OcrCache | None = None,
    ) -> None:
        self._backend = backend
        self._max_workers = self._clamp_workers(max_workers)
        self._threshold = float(confidence_threshold)
        self._cache_enabled = bool(cache_enabled)
        self._cache = _LruCache(cache_max_entries)
        self._hash_fn = hash_fn or _compute_image_hash
        self._disk_cache = disk_cache
        self._log = get_logger(Phase.OCR)

    # ── 属性 ──────────────────────────────────────────────────

    @classmethod
    def _clamp_workers(cls, value: int) -> int:
        """把并发数夹在 ``[1, MAX_WORKERS_LIMIT]``（R8 强制）。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(cls.MAX_WORKERS_LIMIT, n))

    def attach_disk_cache(self, cache: OcrCache | None) -> None:
        """挂载 / 卸载 L2 磁盘缓存（由 ``CheckPipeline`` 在跑批前接线）。

        Args:
            cache: :class:`core.ocr_cache.OcrCache`；``None`` 表示卸载。
        """
        self._disk_cache = cache

    @property
    def disk_cache(self) -> OcrCache | None:
        """返回当前 L2 磁盘缓存（未挂载为 ``None``）。"""
        return self._disk_cache

    @property
    def backend(self) -> OcrBackend:
        """返回底层后端（惰性构造 :class:`RapidOcrBackend`）。"""
        if self._backend is None:
            self._backend = RapidOcrBackend()
        return self._backend

    @property
    def max_workers(self) -> int:
        """实际生效的并发数（已夹在 1–2）。"""
        return self._max_workers

    @property
    def cache(self) -> _LruCache:
        """返回内部缓存（供测试断言命中/未命中）。"""
        return self._cache

    # ── 预热 ──────────────────────────────────────────────────

    def warmup(self) -> None:
        """预热后端模型（幂等；供首启异步加速，架构设计 R2）。"""
        try:
            self.backend.warmup()
        except Exception as exc:  # noqa: BLE001 - 预热失败不致命
            self._log.warning(f"OCR warmup 失败：{exc}")

    def close(self) -> None:
        """释放后端资源。"""
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001 - 关闭失败忽略
            pass

    # ── 单张 ──────────────────────────────────────────────────

    def recognize_one(self, image_path: str | os.PathLike[str]) -> OcrText:
        """识别单张（带两级缓存与容错，**永不抛异常**）。

        缓存查找顺序：**L1 进程内 LRU → L2 磁盘缓存 → 真实 OCR**；
        真实识别成功后回写 L2（+ L1）。缓存 key = 图片内容 sha256。

        Args:
            image_path: 图片路径。

        Returns:
            :class:`core.models.OcrText`；失败时返回低置信占位（``confidence=0.0``
            且 ``low_confidence=True``），并在 ``text_raw`` 标注失败原因。
        """
        p = normalize_path(image_path)
        path_str = str(p)

        cache_key = self._cache_key(path_str)
        content_keyed = bool(cache_key) and not cache_key.startswith("path:")

        # ── L1：进程内 LRU ──
        if self._cache_enabled and cache_key:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._log.debug(f"OCR L1 缓存命中：{p.name}")
                return _with_path(cached, path_str)

        # ── L2：磁盘缓存（仅对内容哈希 key 生效）──
        if self._disk_cache is not None and content_keyed:
            disk = self._disk_cache.get(cache_key, image_path_hint=path_str)
            if disk is not None:
                self._log.debug(f"OCR L2 缓存命中：{p.name}")
                if self._cache_enabled:
                    self._cache.put(cache_key, disk)
                return disk

        # ── 真实识别 ──
        try:
            result = self.backend.recognize(p)
        except Exception as exc:  # noqa: BLE001 - 单张失败不中断（契约要求）
            self._log.warning(f"OCR 失败（已跳过该图）：{path_str} —— {type(exc).__name__}: {exc}")
            return OcrText(
                image_path=path_str,
                text_raw="",
                confidence=0.0,
                boxes=[],
                seq=0,
                low_confidence=True,
            )

        annotated = self._finalize(result, path_str)
        if self._cache_enabled and cache_key:
            self._cache.put(cache_key, annotated)
        if self._disk_cache is not None and content_keyed:
            self._disk_cache.put(cache_key, annotated, image_path_hint=path_str)
        return annotated

    def _cache_key(self, path_str: str) -> str:
        """计算缓存 key（内容哈希；失败时退化为路径）。

        Args:
            path_str: 图片路径字符串。

        Returns:
            缓存 key（内容哈希优先，读失败则用 ``path:`` 前缀的路径 key）。
        """
        try:
            digest = self._hash_fn(path_str)
        except Exception:  # noqa: BLE001 - 哈希失败不影响主流程
            digest = ""
        if digest:
            return digest
        return f"path:{path_str}" if path_str else ""

    def _finalize(self, result: OcrText, path_str: str) -> OcrText:
        """统一归一化 OCR 结果（补路径、标低置信、去空白）。

        Args:
            result: 后端返回的结果。
            path_str: 期望写回的路径字符串。

        Returns:
            归一化后的 :class:`core.models.OcrText`（就地补字段）。
        """
        if not result.image_path:
            result.image_path = path_str
        result.text_raw = result.text_raw or ""
        result.low_confidence = float(result.confidence or 0.0) < self._threshold
        return result

    # ── 批量 ──────────────────────────────────────────────────

    def recognize_batch(
        self,
        paths: list[str | os.PathLike[str]],
        max_workers: int = 2,
    ) -> list[OcrText]:
        """批量识别（**单张失败不中断**，架构设计第 4 节契约）。

        并发策略：``ThreadPoolExecutor(max_workers≤2)``；结果按输入顺序返回。
        单张失败由 :meth:`recognize_one` 兜底为低置信占位，**不会中断整批**。

        Args:
            paths: 图片路径列表。
            max_workers: 并发数（会被夹在 1–2，R8）。

        Returns:
            与 ``paths`` **等长且同序**的 :class:`core.models.OcrText` 列表。
            空输入返回空列表。
        """
        path_list = list(paths or [])
        if not path_list:
            return []

        workers = self._clamp_workers(max_workers)
        if workers <= 1 or len(path_list) == 1:
            return [self.recognize_one(p) for p in path_list]

        results: list[OcrText | None] = [None] * len(path_list)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool:
            future_map = {
                pool.submit(self.recognize_one, p): idx for idx, p in enumerate(path_list)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都不得中断整批
                    self._log.warning(
                        f"OCR 批量任务异常（已占位）：{path_list[idx]} —— "
                        f"{type(exc).__name__}: {exc}"
                    )
                    results[idx] = OcrText(
                        image_path=str(path_list[idx]),
                        text_raw="",
                        confidence=0.0,
                        boxes=[],
                        seq=0,
                        low_confidence=True,
                    )

        # 兜底：理论上不会出现 None，防御性补齐
        return [
            item if item is not None else OcrText(image_path=str(path_list[i]), low_confidence=True)
            for i, item in enumerate(results)
        ]
