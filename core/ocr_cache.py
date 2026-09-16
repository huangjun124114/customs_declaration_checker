"""OCR 结果磁盘缓存（core.ocr_cache，对应 v0.2.0 方案 §3.5「点 5」）。

把单张图的 OCR 结果按 **图片内容哈希**（sha256）落盘，实现「同图只 OCR 一次」——
即便跨进程 / 跨跑批也能复用。本模块是 **L2 磁盘缓存**；
进程内 LRU（L1）仍由 :class:`core.ocr_engine._LruCache` 承担（两级串联，见 ``ocr_engine``）。

落点（**过程产出**，遵守「产物分流」铁律）::

    {process_dir}/ocr_cache/{hash前2位}/{hash}.json

**缓存键 = 图片内容 sha256（非路径）**。即使未来开启降采样，哈希也必须对**原图**
内容计算（否则「原图 → 降采样图」会被误判为同一张）；参数变化通过
``params_fingerprint`` 参与命中判定，避免"旧参数结果被新参数复用"。

**命中条件**（**全部**相等，任一不等即 MISS 并重新 OCR）：
  1. ``image_hash`` 相等；
  2. ``engine.name`` 与 ``engine.version`` 相等；
  3. ``engine_fingerprint``（**次级指纹**：引擎包内文件列表摘要，见下）相等；
  4. ``params_fingerprint``（``resize_long_side`` / ``intra_op_num_threads``）相等。

**次级指纹（批次 3-A 修复 P2）**：``engine.version`` 在打包态（无 ``*.dist-info``）
会降级为 ``"unknown"``，而 ``"unknown" == "unknown"`` 会**误判为命中** → 引擎换版后
静默复用旧结果。故新增 :func:`detect_engine_fingerprint`：对 rapidocr 包内
``(相对路径, size, mtime_ns)`` 列表取 sha256，作为**独立于 version** 的版本校验。
**保守红线**：指纹读不到（空串）时**一律 MISS**（宁可重 OCR，也绝不错误 HIT）。

**容错红线**：
  * 写失败**不得**静默吞掉 —— 记 WARN 但**容忍**（下次跑批会重试）；
  * 读失败（JSON 损坏 / 字段缺失 / schema 未知）→ **MISS + WARN**，**绝不抛异常**；
  * **绝不**删除既有缓存文件（:meth:`OcrCache.clear` 为唯一显式例外）。

``engine.version`` 优先用 ``importlib.metadata.version("rapidocr")``；打包态无
dist-info 时降级为 ``"unknown"`` 且**不抛异常**（历史缺陷 10：打包态读元数据失败）。
降级后缓存有效性仍由 ``image_hash`` + ``params_fingerprint`` 保证 —— 降级只影响
「引擎小版本升级后是否会误用旧结果」这一保守性，不会误用**不同图片**的结果。

分层：``core`` 只能 import ``core`` / ``infra``，**禁止** import PySide6 / PyQt。
本模块**不依赖 cv2 / PIL**：图像尺寸用纯字节头解析（:func:`read_image_size`）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import OcrText
from infra.encoding import normalize_path
from infra.errors import OutputPathViolation
from infra.fs_lock import assert_within
from infra.logger import Phase, get_logger

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "HASH_ALGO",
    "CACHE_DIR_NAME",
    "CacheStats",
    "OcrCache",
    "detect_engine_version",
    "detect_engine_fingerprint",
    "read_image_size",
]

#: 缓存 JSON 的 schema 版本（结构变更须递增；读取时 schema 不符判 MISS）。
CACHE_SCHEMA_VERSION: int = 1

#: 图片哈希算法名（与 :func:`core.ocr_engine._compute_image_hash` 一致）。
HASH_ALGO: str = "sha256"

#: 缓存子目录名（位于 ``{process_dir}`` 之下）。
CACHE_DIR_NAME: str = "ocr_cache"

#: ``engine.version`` 读取失败时的降级值（打包态无 dist-info）。
_UNKNOWN_VERSION: str = "unknown"


def detect_engine_version(dist_name: str = "rapidocr") -> str:
    """尽力读取 OCR 引擎包的版本号；失败降级为 ``"unknown"``（**不抛异常**）。

    打包态（PyInstaller）通常**没有** ``*.dist-info`` 元数据，``importlib.metadata``
    会抛 ``PackageNotFoundError``。历史缺陷 10 的教训是「不要在启动/识别路径抛异常」。

    Args:
        dist_name: 分发包名（默认 ``"rapidocr"``）。

    Returns:
        版本字符串；不可读时返回 ``"unknown"``。
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:  # noqa: BLE001 - 极端环境缺 importlib.metadata
        return _UNKNOWN_VERSION
    try:
        return str(version(dist_name))
    except PackageNotFoundError:
        return _UNKNOWN_VERSION
    except Exception:  # noqa: BLE001 - 元数据损坏等一律降级
        return _UNKNOWN_VERSION


def _package_dir(dist_name: str) -> str:
    """定位分发包的安装目录（读不到返回空串，**不抛异常**）。"""
    try:
        spec = importlib.util.find_spec(dist_name)
    except Exception:  # noqa: BLE001 - 极端环境下 find_spec 可能抛
        return ""
    if spec is None:
        return ""
    locations = list(spec.submodule_search_locations or [])
    if locations:
        return str(locations[0])
    if spec.origin:
        return os.path.dirname(spec.origin)
    return ""


#: 次级指纹缓存（一次计算，进程内复用；避免每次建 OcrCache 都遍历包目录）。
_FINGERPRINT_CACHE: dict[str, str] = {}
_FINGERPRINT_CACHE_LOCK = threading.Lock()


def detect_engine_fingerprint(dist_name: str = "rapidocr") -> str:
    """计算引擎**次级指纹**：包内文件 ``(相对路径, size, mtime_ns)`` 的 sha256。

    用于弥补 ``engine.version`` 在打包态降级为 ``"unknown"`` 时的**命中判定漏洞**
    （``"unknown" == "unknown"`` 会误判为命中 → 引擎换版后静默复用旧结果）。
    只要包内文件发生增删改（含版本升级替换），指纹即变化 → 缓存 MISS。

    ⚠️ **保守红线**：包目录不可定位 / 无文件 / 读取异常 → 返回 ``""``；
    调用方（:class:`OcrCache`）遇到空指纹**一律判 MISS**，绝不因"双方都读不到"
    而误判命中。

    Args:
        dist_name: 分发包名（默认 ``"rapidocr"``）。

    Returns:
        十六进制 sha256；不可读返回空串。

    Note:
        结果按 ``dist_name`` 进程内缓存；遍历包目录仅首次发生（实测 175 个文件，
        毫秒级，对启动无可见影响）。
    """
    with _FINGERPRINT_CACHE_LOCK:
        cached = _FINGERPRINT_CACHE.get(dist_name)
    if cached is not None:
        return cached

    value = _compute_package_fingerprint(dist_name)
    with _FINGERPRINT_CACHE_LOCK:
        _FINGERPRINT_CACHE[dist_name] = value
    return value


def _compute_package_fingerprint(dist_name: str) -> str:
    """遍历包目录计算指纹（失败返回空串）。"""
    pkg = _package_dir(dist_name)
    if not pkg or not os.path.isdir(pkg):
        return ""

    entries: list[tuple[str, int, int]] = []
    try:
        for root, dirs, files in os.walk(pkg):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, pkg).replace("\\", "/")
                entries.append((rel, int(stat.st_size), int(stat.st_mtime_ns)))
    except OSError:
        return ""

    if not entries:
        return ""

    entries.sort()
    digest = hashlib.sha256()
    for rel, size, mtime_ns in entries:
        digest.update(f"{rel}\0{size}\0{mtime_ns}\n".encode())
    return digest.hexdigest()


# ══════════════════════════════════════════════════════════════════
#  统计计数
# ══════════════════════════════════════════════════════════════════


@dataclass
class CacheStats:
    """缓存统计计数（供跑批日志 ``OCR 缓存命中 N / 实际识别 M``）。"""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    corrupt: int = 0

    def as_dict(self) -> dict[str, int]:
        """返回只读字典快照。"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "corrupt": self.corrupt,
        }

    def reset(self) -> None:
        """清零全部计数。"""
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.corrupt = 0


# ══════════════════════════════════════════════════════════════════
#  纯字节头解析图像尺寸（不依赖 cv2 / PIL）
# ══════════════════════════════════════════════════════════════════

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> tuple[int, int]:
    """解析 PNG 尺寸（IHDR 位于文件头，宽度/高度为大端）。"""
    if len(data) < 24 or data[:8] != _PNG_SIG:
        return 0, 0
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """解析 JPEG 尺寸（遍历段找 SOF 标记）。"""
    n = len(data)
    i = 2
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # 独立标记（无长度字段）
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if seg_len < 2:
            break
        # SOF0..SOF15（排除 DHT=C4 / JPG=C8 / DAC=CC）
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + seg_len
    return 0, 0


def _bmp_size(data: bytes) -> tuple[int, int]:
    """解析 BMP 尺寸（小端；高度为负表示自上而下）。"""
    if len(data) < 26 or data[:2] != b"BM":
        return 0, 0
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    return abs(width), abs(height)


def _gif_size(data: bytes) -> tuple[int, int]:
    """解析 GIF 尺寸（小端）。"""
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return 0, 0
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return width, height


def read_image_size(path: str | os.PathLike[str]) -> tuple[int, int]:
    """用**纯字节头解析**读取图片宽高（不依赖 cv2 / PIL，中文路径安全）。

    支持 PNG / JPEG / BMP / GIF；无法识别或读取失败返回 ``(0, 0)``（**不抛异常**）。

    Args:
        path: 图片路径。

    Returns:
        ``(width, height)``；未知或失败为 ``(0, 0)``。
    """
    p = normalize_path(path)
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0, 0

    if not data:
        return 0, 0
    if data[:8] == _PNG_SIG:
        return _png_size(data)
    if data[:2] == b"BM":
        return _bmp_size(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_size(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    # 兜底：WebP / TIFF 等暂不支持尺寸解析（不影响缓存有效性与判定）
    return 0, 0


# ══════════════════════════════════════════════════════════════════
#  JSON 安全化（numpy → 内建类型）
# ══════════════════════════════════════════════════════════════════


def _jsonable_scalar(value: Any) -> Any:
    """把单个标量转为 JSON 可序列化的内建类型。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _jsonable(value: Any) -> Any:
    """递归把 numpy 数组 / 序列转为纯 ``list``（内建标量），供 JSON 落盘。

    rapidocr 的 ``boxes`` 可能是 ``numpy.ndarray``（且元素为 numpy 标量），
    直接 ``json.dump`` 会失败；本函数逐层转换为内建类型。标量转为 ``float``。

    Args:
        value: 任意值。

    Returns:
        纯内建类型（list / float / str / None / bool）。
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        return value if isinstance(value, str) else value.decode("utf-8", "replace")
    try:
        return [_jsonable(item) for item in value]
    except TypeError:
        return _jsonable_scalar(value)


def _now_local_iso() -> str:
    """返回带**本地时区偏移**的 ISO 8601 时间（如 ``2026-09-16T12:15:03+08:00``）。

    ⚠️ 不使用 ``Z``（UTC）：需求要求本地时间偏移，便于人工核对跑批时刻。
    """
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


# ══════════════════════════════════════════════════════════════════
#  缓存主体
# ══════════════════════════════════════════════════════════════════


class OcrCache:
    """OCR 结果磁盘缓存（L2；进程内 L1 见 ``core.ocr_engine``）。

    Args:
        process_dir: 过程产出根目录（缓存落在其下 ``ocr_cache/``）。
        allowed_root: 越界断言用根（缺省为 ``process_dir``）；写入前经
            :func:`infra.fs_lock.assert_within` 校验，越界抛
            :class:`infra.errors.OutputPathViolation`。
        engine_name: OCR 引擎名（参与命中判定）。
        engine_version: 引擎版本；``None`` 时自动探测（失败降级 ``"unknown"``）。
        engine_fingerprint: 引擎**次级指纹**（包内文件摘要）；``None`` 时自动探测
            （:func:`detect_engine_fingerprint`）。**读不到为空串 → 保守判 MISS**。
        resize_long_side: 降采样长边（默认 0=不降采样；参与命中判定）。
        intra_op_num_threads: onnxruntime 单算子线程数（参与命中判定）。
        enabled: 是否启用（``False`` 时 :meth:`get`/:meth:`put` 直接短路）。
    """

    def __init__(
        self,
        process_dir: str | os.PathLike[str],
        *,
        allowed_root: str | os.PathLike[str] | None = None,
        engine_name: str = "rapidocr",
        engine_version: str | None = None,
        engine_fingerprint: str | None = None,
        resize_long_side: int = 0,
        intra_op_num_threads: int = 2,
        enabled: bool = True,
    ) -> None:
        self.process_dir: Path = normalize_path(process_dir)
        self.cache_dir: Path = self.process_dir / CACHE_DIR_NAME
        self._allowed_root: Path = (
            normalize_path(allowed_root) if allowed_root is not None else self.process_dir
        )
        self.engine_name: str = str(engine_name or "rapidocr")
        self.engine_version: str = (
            str(engine_version) if engine_version is not None else detect_engine_version()
        )
        self.engine_fingerprint: str = (
            str(engine_fingerprint)
            if engine_fingerprint is not None
            else detect_engine_fingerprint()
        )
        self.resize_long_side: int = int(resize_long_side)
        self.intra_op_num_threads: int = int(intra_op_num_threads)
        self.enabled: bool = bool(enabled)

        self._stats = CacheStats()
        self._stat_lock = threading.Lock()
        self._log = get_logger(Phase.OCR)

    # ───────────────────── 身份指纹 ─────────────────────

    def params_fingerprint(self) -> dict[str, int]:
        """返回参数指纹（参与命中判定，防止「旧参数结果被新参数复用」）。"""
        return {
            "resize_long_side": self.resize_long_side,
            "intra_op_num_threads": self.intra_op_num_threads,
        }

    def identity(self) -> dict[str, Any]:
        """返回缓存身份（引擎名/版本/次级指纹 + 参数指纹），供日志与追溯。"""
        return {
            "engine": {
                "name": self.engine_name,
                "version": self.engine_version,
                "fingerprint": self.engine_fingerprint,
            },
            "params_fingerprint": self.params_fingerprint(),
        }

    # ───────────────────── 路径 ─────────────────────

    def path_for(self, image_hash: str) -> Path:
        """返回某哈希对应的缓存文件路径 ``{cache_dir}/{hash[:2]}/{hash}.json``。

        Args:
            image_hash: 图片内容哈希（十六进制）。

        Returns:
            目标 :class:`pathlib.Path`（不保证存在）。
        """
        key = (image_hash or "").strip()
        bucket = key[:2] if key else "00"
        return self.cache_dir / bucket / f"{key}.json"

    # ───────────────────── 读（命中判定）─────────────────────

    def get(
        self,
        image_hash: str,
        *,
        image_path_hint: str = "",
    ) -> OcrText | None:
        """尝试命中缓存；任何异常/不符一律返回 ``None``（MISS），**绝不抛异常**。

        Args:
            image_hash: 图片内容哈希。
            image_path_hint: 当前路径（命中时写回 ``OcrText.image_path``，
                因缓存按**内容**键控，同内容可能来自不同路径）。

        Returns:
            命中的 :class:`core.models.OcrText`（``image_path`` 已设为当前路径）；
            MISS 返回 ``None``。
        """
        if not self.enabled or not image_hash:
            return None

        target = self.path_for(image_hash)
        if not target.is_file():
            self._note_miss()
            return None

        try:
            with open(target, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            # 损坏 / 半截 JSON → MISS + WARN（容错红线：不得抛异常）
            self._log.warning(f"OCR 缓存损坏（判 MISS 并重新识别）：{target} —— {exc}")
            self._note_corrupt()
            self._note_miss()
            return None

        reason = self._mismatch_reason(payload, image_hash)
        if reason is not None:
            # 正常未命中（哈希/引擎/参数不符）→ 仅 DEBUG，避免刷屏
            self._log.debug(f"OCR 缓存未命中（{reason}）：{target}")
            self._note_miss()
            return None

        ocr = self._from_payload(payload, image_path_hint)
        if ocr is None:
            self._log.warning(f"OCR 缓存字段缺失/非法（判 MISS）：{target}")
            self._note_miss()
            return None

        self._note_hit()
        return ocr

    def _mismatch_reason(self, payload: Any, image_hash: str) -> str | None:
        """返回不匹配原因；完全匹配返回 ``None``。"""
        if not isinstance(payload, dict):
            return "根节点非字典"
        if payload.get("schema") != CACHE_SCHEMA_VERSION:
            return f"schema 不符({payload.get('schema')!r})"
        if payload.get("hash_algo") != HASH_ALGO:
            return "hash_algo 不符"
        if payload.get("image_hash") != image_hash:
            return "image_hash 不符"
        engine = payload.get("engine")
        if not isinstance(engine, dict):
            return "engine 缺失"
        if engine.get("name") != self.engine_name:
            return "engine.name 不符"
        if engine.get("version") != self.engine_version:
            return "engine.version 不符"
        # ── 次级指纹（P2 修复）：修补 version="unknown" 的命中漏洞 ──
        #    保守红线：本机指纹读不到（空串）→ 一律 MISS，绝不让 unknown==unknown 命中。
        if not self.engine_fingerprint:
            return "engine_fingerprint 不可读（保守 MISS）"
        if payload.get("engine_fingerprint") != self.engine_fingerprint:
            return "engine_fingerprint 不符"
        if payload.get("params_fingerprint") != self.params_fingerprint():
            return "params_fingerprint 不符"
        return None

    def _from_payload(self, payload: dict[str, Any], image_path_hint: str) -> OcrText | None:
        """把缓存 JSON 还原为 :class:`core.models.OcrText`；结构非法返回 ``None``。

        ``confidence`` 由 ``lines[].score`` 的最小值**重算**（与后端一致），
        从而在不额外存储字段的前提下无损还原（符合 §3.5 严格 schema）。
        """
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list):
            return None

        texts: list[str] = []
        boxes: list[Any] = []
        scores: list[float] = []
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text is None:
                continue
            texts.append(str(text))
            box = item.get("box")
            if box is not None:
                boxes.append(box)
            try:
                scores.append(float(item.get("score", 0.0)))
            except (TypeError, ValueError):
                scores.append(0.0)

        text_raw = payload.get("text_raw")
        if not isinstance(text_raw, str):
            text_raw = "\n".join(texts)

        raw_kv = payload.get("kv")
        kv = {str(k): str(v) for k, v in raw_kv.items()} if isinstance(raw_kv, dict) else {}

        confidence = min(scores) if scores else 0.0
        path = str(image_path_hint or payload.get("image_path_hint") or "")
        return OcrText(
            image_path=path,
            text_raw=text_raw,
            confidence=confidence,
            boxes=boxes,
            seq=0,
            low_confidence=bool(payload.get("low_confidence", False)),
            line_scores=scores,
            kv=kv,
        )

    # ───────────────────── 写 ─────────────────────

    def put(
        self,
        image_hash: str,
        ocr: OcrText,
        *,
        image_path_hint: str | None = None,
        image_size: tuple[int, int] | None = None,
    ) -> None:
        """把 OCR 结果原子写入缓存（``*.tmp`` → ``os.replace``）。

        ⚠️ 多线程写同一哈希**无需加锁**：临时文件名含 pid + 线程号 + 计数，
        最终 ``os.replace`` 原子生效，任一写者结果均可。

        Args:
            image_hash: 图片内容哈希。
            ocr: OCR 结果。
            image_path_hint: 写入时的当前路径（仅作线索存留）。
            image_size: ``(width, height)``；``None`` 时从 ``image_path_hint`` 解析。

        Raises:
            OutputPathViolation: 目标越界（**红线，上抛**，不吞）。
        """
        if not self.enabled or not image_hash or ocr is None:
            return

        target = self.path_for(image_hash)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 产物分流铁律：写前断言落在允许根内（越界抛异常）
            assert_within(target, self._allowed_root)
            payload = self._build_payload(image_hash, ocr, image_path_hint, image_size)
            tmp = self._tmp_path(target)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        except OutputPathViolation:
            raise  # 越界是红线：必须上抛，绝不静默
        except (OSError, TypeError, ValueError) as exc:
            # 写失败容忍（不吞）：记 WARN，下次跑批会重试
            self._log.warning(f"OCR 缓存写入失败（已容忍，下次重试）：{target} —— {exc}")
            return

        self._note_write()

    def _tmp_path(self, target: Path) -> Path:
        """返回唯一临时文件名（避免并发同哈希写竞争）。"""
        tag = f"{os.getpid()}_{threading.get_ident()}"
        return target.with_name(f".{target.name}.{tag}.tmp")

    def _build_payload(
        self,
        image_hash: str,
        ocr: OcrText,
        image_path_hint: str | None,
        image_size: tuple[int, int] | None,
    ) -> dict[str, Any]:
        """构造缓存 JSON（严格遵循方案 §3.5 schema）。"""
        text_lines = ocr.lines()
        boxes = list(ocr.boxes or [])
        scores = list(ocr.line_scores or [])
        lines: list[dict[str, Any]] = []
        for idx, text in enumerate(text_lines):
            box = _jsonable(boxes[idx]) if idx < len(boxes) else None
            score = float(scores[idx]) if idx < len(scores) else float(ocr.confidence or 0.0)
            lines.append({"text": text, "score": score, "box": box})

        if image_size is None:
            size = read_image_size(image_path_hint) if image_path_hint else (0, 0)
        else:
            size = image_size

        return {
            "schema": CACHE_SCHEMA_VERSION,
            "hash_algo": HASH_ALGO,
            "image_hash": image_hash,
            "image_size": [int(size[0]), int(size[1])],
            "image_path_hint": str(image_path_hint or ""),
            "engine": {"name": self.engine_name, "version": self.engine_version},
            "engine_fingerprint": self.engine_fingerprint,
            "params_fingerprint": self.params_fingerprint(),
            "created_at": _now_local_iso(),
            "lines": lines,
            "kv": {str(k): str(v) for k, v in (ocr.kv or {}).items()},
            "text_raw": ocr.text_raw or "",
            "low_confidence": bool(ocr.low_confidence),
        }

    # ───────────────────── 维护 ─────────────────────

    def clear(self) -> int:
        """清空缓存（菜单「清空 OCR 缓存」唯一显式删除入口）。

        Returns:
            删除的文件数（``.json`` 与残留 ``.tmp``）。
        """
        if not self.cache_dir.is_dir():
            return 0
        # 删除前同样断言归属（防御式）
        assert_within(self.cache_dir, self._allowed_root)

        removed = 0
        for pattern in ("*.json", "*.tmp"):
            try:
                candidates = list(self.cache_dir.rglob(pattern))
            except OSError as exc:
                self._log.warning(f"OCR 缓存列举失败：{exc}")
                break
            for path in candidates:
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError as exc:
                    self._log.warning(f"OCR 缓存文件删除失败（已跳过）：{path} —— {exc}")
        self._log.info(f"OCR 缓存已清空（删除 {removed} 个文件）：{self.cache_dir}")
        return removed

    def size(self) -> int:
        """返回当前缓存文件数（``.json``）。"""
        if not self.cache_dir.is_dir():
            return 0
        try:
            return sum(1 for _ in self.cache_dir.rglob("*.json"))
        except OSError:
            return 0

    def stats(self) -> dict[str, int]:
        """返回统计快照（hits / misses / writes / corrupt）。"""
        with self._stat_lock:
            return self._stats.as_dict()

    def reset_stats(self) -> None:
        """清零统计计数。"""
        with self._stat_lock:
            self._stats.reset()

    # ───────────────────── 计数（线程安全）─────────────────────

    def _note_hit(self) -> None:
        with self._stat_lock:
            self._stats.hits += 1

    def _note_miss(self) -> None:
        with self._stat_lock:
            self._stats.misses += 1

    def _note_write(self) -> None:
        with self._stat_lock:
            self._stats.writes += 1

    def _note_corrupt(self) -> None:
        with self._stat_lock:
            self._stats.corrupt += 1
