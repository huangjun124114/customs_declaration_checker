"""应用配置（infra.config，对应架构设计 3 节 + 12.C.3 + 12.D.2 + 12.E.1/E.3）。

:class:`AppConfig` 承载：
  * **默认共享根**（含 ``{年份}`` 占位，模板 :data:`SHARE_BASE_TEMPLATE`）；
  * **输出落点**（成果产出 / 过程产出，强制分流）；
  * **批大小阈值**（默认 6 条/批，图片多降 3 条）；
  * **OCR 参数**（线程数、置信度阈值、缓存开关）；
  * **路径记忆**（v1.1 E1，FR-024 提前 P0）：持久化到
    ``%APPDATA%\\CustomsChecker\\config.json``。

另有 :meth:`AppConfig.from_env` 内部通道（Q4）：读取
``CUSTOMS_EXCEL`` / ``CUSTOMS_SHARE`` / ``MAX_RECORDS`` / ``RESUME_LOG`` / ``ACCUM_LOG``
供回归测试与「引擎独立跑」复用，**不作为用户主路径、不在 UI 暴露**。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, ClassVar

from infra.encoding import normalize_path, normalize_path_text, read_json, write_json
from infra.resources import user_config_dir

__all__ = [
    "SHARE_BASE_TEMPLATE",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_BATCH_SIZE_HEAVY",
    "DEFAULT_OCR_CONFIDENCE_THRESHOLD",
    "AppConfig",
    "default_share_root",
]

#: 共享根模板（含 ``{年份}`` 占位 —— SOP 2.6「迁移必改三处」之一，跨年必须改）
SHARE_BASE_TEMPLATE: str = (
    r"\\172.20.99.220\制造中心\仓储物流部\成品科\{年份}年报关要素图片"
)

#: 常规批大小（条/批，SOP Phase 3）
DEFAULT_BATCH_SIZE: int = 6
#: 图片多的票降批（电源板/主板 7–12 张图）
DEFAULT_BATCH_SIZE_HEAVY: int = 3
#: 单条记录图片数超过该值时自动降批
HEAVY_IMAGE_THRESHOLD: int = 8
#: 低置信度阈值（低于则 NoiseGuard 优先视为 SUSPICIOUS）
DEFAULT_OCR_CONFIDENCE_THRESHOLD: float = 0.5

#: 环境变量通道名（Q4，内部通道）
ENV_EXCEL = "CUSTOMS_EXCEL"
ENV_SHARE = "CUSTOMS_SHARE"
ENV_MAX_RECORDS = "MAX_RECORDS"
ENV_RESUME_LOG = "RESUME_LOG"
ENV_ACCUM_LOG = "ACCUM_LOG"


def default_share_root(year: int | None = None) -> str:
    """按年份生成默认共享根。

    Args:
        year: 年份；``None`` 时用当前年份。

    Returns:
        已替换 ``{年份}`` 的绝对/UNC 路径字符串。
    """
    if year is None:
        import datetime as _dt

        year = _dt.date.today().year
    return SHARE_BASE_TEMPLATE.replace("{年份}", str(year))


def _config_file_path() -> Path:
    """返回配置文件路径 ``%APPDATA%\\CustomsChecker\\config.json``。"""
    return user_config_dir() / "config.json"


@dataclass
class AppConfig:
    """应用配置（一次会话的全部可调参数）。

    所有字段都有默认值，保证「零配置可启动」。用户指定字段优先级最高
    （对应 SOP 2.2：绝不静默扫描默认目录）。

    Attributes:
        excel_path: 用户选定的申报要素 Excel 路径（可 UNC，可为空）。
        share_root: 图片根目录。按方案 3（N5）推荐**直接选到票号目录**；
            也允许选到 ``{年份}年报关要素图片`` 层，由 PathPolicy 自动下探。
        share_year: 年份（用于 ``SHARE_BASE_TEMPLATE`` 的高级折叠区预置）。
        ticket_no: 票号（出货通知书号）。自动识别失败时 UI 先按图片根末段推断、
            推断不出再弹框手填（Q2；v0.2.0，不再强制预填）。
        result_dir: **兼容占位**（v0.2.0 起不读取、不写入生效值；输出目录由
            ``app.path_policy.PathPolicy.resolve_outputs`` 的运行目录约定决定）。
        process_dir: **兼容占位**（同上）。
        batch_size: 常规批大小（条/批）。
        batch_size_heavy: 图片多时的降批大小。
        heavy_image_threshold: 单条记录图片数超过该值时降批。
        ocr_max_workers: OCR 同条记录多图并行数（≤2，避免线程爆炸，R8）。
        ocr_intra_op_threads: onnxruntime 单算子线程数（R8，建议 2）。
        ocr_confidence_threshold: 低置信度阈值（低于则视为 SUSPICIOUS）。
        ocr_cache_enabled: 是否启用按图片哈希的 OCR 结果缓存。
        max_records: 本批处理记录上限（0 表示全量；对应 ``MAX_RECORDS``）。
        last_run_ts: 上次运行时间戳（ISO 8601，供 UI 展示）。
        window_geometry: 上次窗口几何（``x,y,w,h`` 字符串，供 UI 记忆）。
    """

    # ── 数据源（用户指定优先）──
    excel_path: str = ""
    share_root: str = ""
    share_year: int = 0
    ticket_no: str = ""

    # ── 输出落点（**v0.2.0 起降级为兼容占位**）──
    #
    # ⚠️ Q8 决策：输出目录**不再由配置决定**，改由运行目录约定
    # （``app_base_dir()/报关申报要素校验/{result,logs}``，见
    # ``app.path_policy.PathPolicy.resolve_outputs``）自动就位。
    # 这两个字段**保留仅为兼容旧版 config.json 反序列化**（防 ``from_dict`` 崩溃），
    # 且**不被读取、不被写入生效值**——UI 不再写回它们（见 DataSourcePanel.apply_to_config）。
    result_dir: str = ""
    process_dir: str = ""

    # ── 批处理 ──
    batch_size: int = DEFAULT_BATCH_SIZE
    batch_size_heavy: int = DEFAULT_BATCH_SIZE_HEAVY
    heavy_image_threshold: int = HEAVY_IMAGE_THRESHOLD

    # ── OCR ──
    ocr_max_workers: int = 2
    ocr_intra_op_threads: int = 2
    ocr_confidence_threshold: float = DEFAULT_OCR_CONFIDENCE_THRESHOLD
    ocr_cache_enabled: bool = True

    # ── 其它 ──
    max_records: int = 0

    # ── 会话记忆 ──
    last_run_ts: str = ""
    window_geometry: str = ""

    #: 配置文件版本（升级时用于迁移）
    SCHEMA_VERSION: ClassVar[int] = 1

    # ─────────────────────── 派生属性 ───────────────────────

    @property
    def has_excel(self) -> bool:
        """是否已指定 Excel 路径。"""
        return bool(self.excel_path.strip())

    @property
    def has_share_root(self) -> bool:
        """是否已指定图片根目录。"""
        return bool(self.share_root.strip())

    @property
    def needs_user_input(self) -> bool:
        """是否仍需用户指定数据源（对应 SOP 2.2「绝不盲目扫描」）。

        Returns:
            ``True`` 表示 Excel 或图片根缺失，UI 应提示用户选择而非静默扫默认目录。
        """
        return not (self.has_excel and self.has_share_root)

    def resolved_share_root(self) -> str:
        """返回实际使用的图片根：用户指定优先，否则按年份生成模板路径。

        Returns:
            图片根路径字符串（可能是 UNC）。
        """
        if self.has_share_root:
            return self.share_root
        return default_share_root(self.share_year or None)

    def effective_batch_size(self, image_count: int) -> int:
        """按单条记录图片数决定批大小（SOP Phase 3 降批规则）。

        Args:
            image_count: 当前记录的图片张数。

        Returns:
            建议批大小。
        """
        if image_count >= self.heavy_image_threshold:
            return max(1, self.batch_size_heavy)
        return max(1, self.batch_size)

    # ─────────────────────── 持久化（E1 路径记忆）───────────────────────

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 落盘的字典（路径保持用户输入原样，UNC 不被改写）。"""
        data = asdict(self)
        for key in ("excel_path", "share_root", "result_dir", "process_dir"):
            value = data.get(key, "")
            data[key] = normalize_path_text(value) if value else ""
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """从字典构造配置（忽略未知键，容忍旧版本 schema）。

        Args:
            data: 配置字典。

        Returns:
            :class:`AppConfig` 实例。
        """
        known = {f.name for f in fields(cls)}
        filtered: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key in known:
                filtered[key] = value
        try:
            return cls(**filtered)
        except TypeError:
            # 类型不匹配时退回全默认，保证启动不因脏配置而崩
            return cls()

    def save(self, path: str | Path | None = None) -> Path:
        """持久化配置到 ``%APPDATA%\\CustomsChecker\\config.json``。

        Args:
            path: 自定义路径（主要供测试）；缺省用用户配置目录。

        Returns:
            实际写入的 :class:`pathlib.Path`。
        """
        target = normalize_path(path) if path is not None else _config_file_path()
        payload = {"schema": self.SCHEMA_VERSION, "config": self.to_dict()}
        return write_json(target, payload)

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        """从磁盘加载配置；文件缺失 / 损坏时返回全默认配置（启动永不失败）。

        Args:
            path: 自定义路径（主要供测试）；缺省用用户配置目录。

        Returns:
            :class:`AppConfig` 实例（保证非 ``None``）。
        """
        target = normalize_path(path) if path is not None else _config_file_path()
        raw = read_json(target, default={})
        if not isinstance(raw, dict):
            return cls()
        # 兼容「带 schema 包装」与「裸配置」两种形态
        if "config" in raw and isinstance(raw.get("config"), dict):
            return cls.from_dict(raw["config"])
        return cls.from_dict(raw)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        """从环境变量构造配置（Q4 内部通道，供回归/自动化使用）。

        读取 ``CUSTOMS_EXCEL`` / ``CUSTOMS_SHARE`` / ``MAX_RECORDS``
        / ``RESUME_LOG`` / ``ACCUM_LOG``。**不作为用户主路径**。

        Args:
            env: 环境变量字典；缺省读 ``os.environ``。

        Returns:
            :class:`AppConfig` 实例。
        """
        source = env if env is not None else dict(os.environ)
        cfg = cls()
        excel = source.get(ENV_EXCEL, "").strip()
        if excel:
            cfg.excel_path = normalize_path_text(excel)
        share = source.get(ENV_SHARE, "").strip()
        if share:
            cfg.share_root = normalize_path_text(share)
        max_records = source.get(ENV_MAX_RECORDS, "").strip()
        if max_records.isdigit():
            cfg.max_records = int(max_records)
        return cfg
