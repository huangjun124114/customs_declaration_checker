"""规则仓库（core.rule_repository，对应架构设计 12.C）。

职责：
  * :meth:`RuleRepository.load_all` —— 启动时**一次性**读入 ``rules/*.yaml``，
    冻结为**不可变快照**（``@dataclass(frozen=True)``）；
  * :meth:`RuleRepository.validate` —— 校验 YAML 语法 + 必填字段，
    失败抛 :class:`RuleConfigError`（含字段名 + 文件），**不静默降级**；
  * :meth:`RuleRepository.reload` —— 手动重载（菜单「重载规则文件」用），
    **不做自动热加载**（跑批中改规则会违反可追溯性）；
  * :meth:`RuleRepository.get` —— 取某份规则快照。

优先级：``%APPDATA%\\CustomsChecker\\rules\\`` 用户外置覆盖 > 内置 ``rules/``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra.encoding import normalize_path
from infra.errors import RuleConfigError
from infra.resources import resource_path, user_rules_dir

__all__ = [
    "RULE_FILES",
    "FieldBlacklistRules",
    "BrandPatternRules",
    "ModelCleanRules",
    "WholeMachineBrandRules",
    "SeparatorRules",
    "NoiseSignalRules",
    "NonBrandTokenRules",
    "OcrKvPatternRules",
    "RuleSet",
    "RuleRepository",
]

#: 规则文件名清单（v1.1 C.2；缺陷 C 追加 ``non_brand_tokens.yaml``；
#: v0.2.0 批次 2 追加 ``ocr_kv_patterns.yaml``，点 8 的 KV 规则外置载体）。
RULE_FILES: tuple[str, ...] = (
    "fields_blacklist.yaml",
    "brand_patterns.yaml",
    "model_clean_rules.yaml",
    "whole_machine_brand.yaml",
    "separators.yaml",
    "noise_signals.yaml",
    "non_brand_tokens.yaml",
    "ocr_kv_patterns.yaml",
)


# ══════════════════════════════════════════════════════════════════
#  不可变规则快照（每份 YAML 一个 frozen dataclass）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FieldBlacklistRules:
    """``fields_blacklist.yaml`` 快照。"""

    terms: tuple[str, ...] = ()
    skip_prefixes: tuple[str, ...] = ()
    source_path: str = ""


@dataclass(frozen=True)
class BrandPatternRules:
    """``brand_patterns.yaml`` 快照。"""

    patterns: tuple[tuple[str, str, str], ...] = ()   # (name, regex, note)
    none_tokens: tuple[str, ...] = ()
    source_path: str = ""

    def compiled(self) -> list[tuple[str, re.Pattern[str], str]]:
        """编译正则（每次调用返回新列表，避免共享可变状态）。

        Returns:
            ``[(name, compiled_pattern, note), ...]``。
        """
        result: list[tuple[str, re.Pattern[str], str]] = []
        for name, regex, note in self.patterns:
            result.append((name, re.compile(regex), note))
        return result


@dataclass(frozen=True)
class ModelCleanRules:
    """``model_clean_rules.yaml`` 快照。"""

    anchor_regex: str = r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{1,39}"
    dirty_tail_chars: tuple[str, ...] = ()
    protect_patterns: tuple[str, ...] = ()
    model_field_names: tuple[str, ...] = ()
    source_path: str = ""

    def compiled_anchor(self) -> re.Pattern[str]:
        """编译头部锚定正则。"""
        return re.compile(self.anchor_regex)

    def compiled_protect(self) -> list[re.Pattern[str]]:
        """编译保护模式正则列表。"""
        return [re.compile(p) for p in self.protect_patterns]


@dataclass(frozen=True)
class WholeMachineBrandRules:
    """``whole_machine_brand.yaml`` 快照。"""

    context_tokens: tuple[str, ...] = ()
    context_window: int = 3
    context_scope: str = "whole_image"
    verdict_hint: str = ""
    source_path: str = ""


@dataclass(frozen=True)
class NonBrandTokenRules:
    """``non_brand_tokens.yaml`` 快照（缺陷 C 裁决，架构裁决文档 4.3）。

    这些 token 在图片侧出现时**不得**被当作品牌值（单位 / 通用词 / 元件厂标 / 应用名）。
    """

    tokens: tuple[str, ...] = ()
    source_path: str = ""


@dataclass(frozen=True)
class OcrKvPatternRules:
    """``ocr_kv_patterns.yaml`` 快照（v0.2.0 点 8）。

    KV（键值对）提取规则：字段别名表 / 分隔符集合 / 弱分隔阈值 / 噪声词表。
    ⚠️ KV **不参与判定**；本快照仅供 :class:`core.kv_extractor.KvExtractor` 使用。

    Attributes:
        field_aliases: ``((规范字段名, (别名, ...)), ...)``；用于识别"是否字段键"。
        separators: 键值分隔符（半角/全角冒号、等号等）。
        whitespace_separator_min: 连续空白视为弱分隔的最小空格数。
        noise_terms: KV 级噪声词（整行命中则判为噪声，不进 KV）。
        allow_next_line_value: 是否支持「值在下一行」（默认 ``False``）。
        source_path: 实际加载路径（追溯用）。
    """

    field_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    separators: tuple[str, ...] = ()
    whitespace_separator_min: int = 2
    noise_terms: tuple[str, ...] = ()
    allow_next_line_value: bool = False
    source_path: str = ""

    def alias_index(self) -> dict[str, str]:
        """返回 ``{归一化别名 → 规范字段名}`` 索引（每次调用返回新字典）。"""
        index: dict[str, str] = {}
        for name, aliases in self.field_aliases:
            index.setdefault(name.strip().upper(), name)
            for alias in aliases:
                index.setdefault(alias.strip().upper(), name)
        return index


@dataclass(frozen=True)
class SeparatorRules:
    """``separators.yaml`` 快照。"""

    separators: tuple[str, ...] = ()
    canonical_separator: str = "|"
    colon_variants: tuple[str, ...] = ()
    canonical_colon: str = ":"
    invisible_chars: tuple[str, ...] = ()
    source_path: str = ""


@dataclass(frozen=True)
class NoiseSignalRules:
    """``noise_signals.yaml`` 快照。"""

    confusable_chars: tuple[tuple[str, ...], ...] = ()
    fuzzy_max_edit_distance: int = 2
    fuzzy_min_length: int = 5
    fuzzy_verdict: str = "SUSPICIOUS"
    low_confidence_threshold: float = 0.5
    fragment_min_chars: int = 2
    known_noise_samples: tuple[dict[str, str], ...] = ()
    source_path: str = ""

    def normalize_confusables(self, text: str) -> str:
        """按易混字符表归一化文本（等价类合并，保证对称一致）。

        **实现要点**：易混字符是**等价关系**而非单向替换 —— 例如 ``H`` 同时出现在
        ``["W","H"]`` 与 ``["N","H"]`` 两组中。若按顺序单向替换（H→W 后再 H→N）
        会得到不对称结果（``P/N``→``P/N`` 但 ``P/H``→``P/W``）。

        本实现用**并查集**把同一连通分量内的字符归并到统一代表字符，
        保证：
          * ``SKYWORTH`` 与 ``SKYHORTH`` 归一化后相等（W/H 同组）；
          * ``P/N`` 与 ``P/H`` 归一化后相等（N/H 同组，H 为桥接字符）；
          * 互不相干的字符串（如 ``GXD-009`` vs ``ABC-777``）仍不相等。

        > 说明：因为 ``H`` 是桥接字符，``W`` / ``N`` / ``H`` 会归入同一等价类，
        > 这会让少量字符串被判"疑似相似"。这是**安全方向**的误差 ——
        > 依据「不虚高」红线，疑似噪声只会降级为 ⚠️，绝不会直达 ❌。

        Args:
            text: 原始文本。

        Returns:
            归一化后的文本（大写化 + 等价类字符统一为代表字符）。
        """
        mapping = self._confusable_map()
        return "".join(mapping.get(ch, ch) for ch in (text or "").upper())

    def _confusable_map(self) -> dict[str, str]:
        """构建「字符 → 等价类代表字符」映射（并查集，带缓存）。

        Returns:
            形如 ``{"H": "W", "N": "W", "0": "O", ...}`` 的映射。
        """
        cached = getattr(self, "_confusable_cache", None)
        if cached is not None:
            return cached

        parent: dict[str, str] = {}

        def find(node: str) -> str:
            root = node
            while parent.get(root, root) != root:
                root = parent.get(root, root)
            # 路径压缩
            while parent.get(node, node) != node:
                nxt = parent.get(node, node)
                parent[node] = root
                node = nxt
            return root

        def union(a: str, b: str) -> None:
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                # 统一挂到字典序较小者，保证结果稳定可复现
                if root_a <= root_b:
                    parent[root_b] = root_a
                else:
                    parent[root_a] = root_b

        for group in self.confusable_chars:
            if not group:
                continue
            members = [ch.upper() for ch in group]
            first = members[0]
            for ch in members[1:]:
                union(first, ch)

        result: dict[str, str] = {}
        for ch in parent:
            result[ch] = find(ch)

        object.__setattr__(self, "_confusable_cache", result)
        return result


@dataclass(frozen=True)
class RuleSet:
    """全部规则快照的聚合（不可变）。"""

    fields_blacklist: FieldBlacklistRules = field(default_factory=FieldBlacklistRules)
    brand_patterns: BrandPatternRules = field(default_factory=BrandPatternRules)
    model_clean_rules: ModelCleanRules = field(default_factory=ModelCleanRules)
    whole_machine_brand: WholeMachineBrandRules = field(
        default_factory=WholeMachineBrandRules
    )
    separators: SeparatorRules = field(default_factory=SeparatorRules)
    noise_signals: NoiseSignalRules = field(default_factory=NoiseSignalRules)
    non_brand_tokens: NonBrandTokenRules = field(default_factory=NonBrandTokenRules)
    ocr_kv_patterns: OcrKvPatternRules = field(default_factory=OcrKvPatternRules)

    def sources(self) -> dict[str, str]:
        """返回 ``{规则名: 实际加载路径}``（供日志与追溯）。"""
        return {
            "fields_blacklist": self.fields_blacklist.source_path,
            "brand_patterns": self.brand_patterns.source_path,
            "model_clean_rules": self.model_clean_rules.source_path,
            "whole_machine_brand": self.whole_machine_brand.source_path,
            "separators": self.separators.source_path,
            "noise_signals": self.noise_signals.source_path,
            "non_brand_tokens": self.non_brand_tokens.source_path,
            "ocr_kv_patterns": self.ocr_kv_patterns.source_path,
        }


# ══════════════════════════════════════════════════════════════════
#  规则仓库
# ══════════════════════════════════════════════════════════════════


class RuleRepository:
    """规则加载 / 校验 / 重载 / 用户外置覆盖。

    典型用法::

        repo = RuleRepository()
        repo.load_all()          # 启动一次性加载（不可变快照）
        rules = repo.get()       # 注入 ElementParser / NoiseGuard
        # ... 跑批 ...
        repo.reload()            # 菜单「重载规则文件」手动触发

    Attributes:
        builtin_dir: 内置规则目录（打包态经 ``resource_path``，开发态为工程 ``rules/``）。
        user_dir: 用户外置规则目录（``%APPDATA%\\CustomsChecker\\rules``）。
    """

    def __init__(
        self,
        builtin_dir: str | Path | None = None,
        user_dir: str | Path | None = None,
    ) -> None:
        """构造规则仓库。

        Args:
            builtin_dir: 内置规则目录；缺省用 ``resource_path("rules")``。
            user_dir: 用户外置目录；缺省用 ``user_rules_dir()``。
        """
        self.builtin_dir: Path = (
            normalize_path(builtin_dir) if builtin_dir is not None else resource_path("rules")
        )
        self.user_dir: Path = (
            normalize_path(user_dir) if user_dir is not None else user_rules_dir()
        )
        self._snapshot: RuleSet | None = None
        self._load_notes: list[str] = []

    # ─────────────────────── 加载 ───────────────────────

    def load_all(self) -> RuleSet:
        """加载全部规则并冻结为不可变快照。

        Returns:
            :class:`RuleSet` 不可变快照。

        Raises:
            RuleConfigError: YAML 语法错误或必填字段缺失。
        """
        notes: list[str] = []
        loaded: dict[str, Any] = {}

        for file_name in RULE_FILES:
            path = self._resolve_file(file_name, notes)
            data = self._read_yaml(path, file_name)
            loaded[file_name] = (data, str(path))

        try:
            snapshot = RuleSet(
                fields_blacklist=self._build_fields_blacklist(*loaded["fields_blacklist.yaml"]),
                brand_patterns=self._build_brand_patterns(*loaded["brand_patterns.yaml"]),
                model_clean_rules=self._build_model_clean(*loaded["model_clean_rules.yaml"]),
                whole_machine_brand=self._build_whole_machine(
                    *loaded["whole_machine_brand.yaml"]
                ),
                separators=self._build_separators(*loaded["separators.yaml"]),
                noise_signals=self._build_noise_signals(*loaded["noise_signals.yaml"]),
                non_brand_tokens=self._build_non_brand_tokens(
                    *loaded["non_brand_tokens.yaml"]
                ),
                ocr_kv_patterns=self._build_ocr_kv_patterns(
                    *loaded["ocr_kv_patterns.yaml"]
                ),
            )
        except RuleConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成业务异常
            raise RuleConfigError(
                f"failed to build rule snapshot: {exc}",
                path=str(self.builtin_dir),
                field="<unknown>",
            ) from exc

        self._snapshot = snapshot
        self._load_notes = notes
        return snapshot

    def validate(self) -> list[str]:
        """校验规则完整性（YAML 语法 + 必填字段）。

        若尚未加载，会先加载。

        Returns:
            警告列表（空列表表示完全通过）。**致命问题一律抛异常**，
            返回的仅是"非致命提示"。

        Raises:
            RuleConfigError: 存在必填字段缺失 / 语法错误。
        """
        snapshot = self._snapshot if self._snapshot is not None else self.load_all()
        warnings: list[str] = []

        if not snapshot.fields_blacklist.terms:
            raise RuleConfigError(
                "fields_blacklist.terms 为空：字段名黑名单是 SOP 3.5 规则#1 的必要载体",
                path=snapshot.fields_blacklist.source_path,
                field="terms",
            )
        if not snapshot.brand_patterns.patterns:
            raise RuleConfigError(
                "brand_patterns.patterns 为空：品牌正则是 SOP 3.5 规则#6 的必要载体",
                path=snapshot.brand_patterns.source_path,
                field="patterns",
            )
        if not snapshot.separators.separators:
            raise RuleConfigError(
                "separators.separators 为空：分隔符归一化表缺失",
                path=snapshot.separators.source_path,
                field="separators",
            )
        if not snapshot.noise_signals.confusable_chars:
            raise RuleConfigError(
                "noise_signals.confusable_chars 为空：v1.2 易混字符表缺失（SOP 陷阱#6）",
                path=snapshot.noise_signals.source_path,
                field="confusable_chars",
            )

        # 校验品牌正则语法
        for name, regex, _note in snapshot.brand_patterns.patterns:
            try:
                re.compile(regex)
            except re.error as exc:
                raise RuleConfigError(
                    f"品牌正则语法错误：{exc}",
                    path=snapshot.brand_patterns.source_path,
                    field=f"patterns.{name}",
                ) from exc

        # 校验型号锚定正则语法
        try:
            snapshot.model_clean_rules.compiled_anchor()
        except re.error as exc:
            raise RuleConfigError(
                f"型号锚定正则语法错误：{exc}",
                path=snapshot.model_clean_rules.source_path,
                field="anchor_regex",
            ) from exc

        if snapshot.noise_signals.fuzzy_verdict != "SUSPICIOUS":
            warnings.append(
                "noise_signals.fuzzy_similarity.verdict 非 SUSPICIOUS —— "
                "违反「不虚高」红线，疑似噪声必须降级为 ⚠️，禁止直达 ❌"
            )

        return warnings

    def reload(self) -> RuleSet:
        """手动重载规则（菜单「重载规则文件」）。

        对应架构设计 12.C.3：**不做自动热加载**；重载后由调用方提示
        "规则已重载，将在下次跑批生效"。

        Returns:
            新的 :class:`RuleSet` 快照。
        """
        return self.load_all()

    def get(self) -> RuleSet:
        """取当前规则快照（未加载则自动加载）。

        Returns:
            :class:`RuleSet` 不可变快照。
        """
        if self._snapshot is None:
            return self.load_all()
        return self._snapshot

    @property
    def load_notes(self) -> list[str]:
        """最近一次加载的说明（含用户外置覆盖提示）。"""
        return list(self._load_notes)

    def rules_source(self, name: str) -> str:
        """返回某份规则的实际来源路径（用户外置 / 内置）。

        Args:
            name: 规则名（不含 ``.yaml``），如 ``"noise_signals"``。

        Returns:
            路径字符串；不存在返回空串。
        """
        return self.get().sources().get(name, "")

    # ─────────────────────── 内部实现 ───────────────────────

    def _resolve_file(self, file_name: str, notes: list[str]) -> Path:
        """解析规则文件路径（用户外置优先）。

        Args:
            file_name: 规则文件名。
            notes: 说明收集列表（原地追加）。

        Returns:
            实际使用的路径。

        Raises:
            RuleConfigError: 内置与用户目录均找不到该文件。
        """
        user_candidate = self.user_dir / file_name
        if user_candidate.is_file():
            notes.append(f"规则 {file_name} 使用用户外置覆盖：{user_candidate}")
            return user_candidate

        builtin_candidate = self.builtin_dir / file_name
        if builtin_candidate.is_file():
            return builtin_candidate

        raise RuleConfigError(
            f"规则文件缺失：{file_name}（内置目录 {self.builtin_dir} 与用户目录 "
            f"{self.user_dir} 均未找到）",
            path=str(self.builtin_dir),
            field=file_name,
        )

    def _read_yaml(self, path: Path, file_name: str) -> dict[str, Any]:
        """读取并解析 YAML 文件。

        Args:
            path: 文件路径。
            file_name: 文件名（错误信息用）。

        Returns:
            解析后的字典。

        Raises:
            RuleConfigError: 文件不可读、YAML 语法错误，或根节点不是字典。
        """
        try:
            import yaml  # 延迟 import：PyYAML 是运行时依赖（rapidocr 亦需要）
        except ImportError as exc:  # pragma: no cover - 环境缺依赖时的明确报错
            raise RuleConfigError(
                "缺少 PyYAML 依赖，无法加载规则文件。请执行 pip install PyYAML",
                path=str(path),
                field=file_name,
            ) from exc

        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise RuleConfigError(
                f"YAML 语法错误：{exc}",
                path=str(path),
                field=file_name,
            ) from exc
        except OSError as exc:
            raise RuleConfigError(
                f"规则文件不可读：{exc}",
                path=str(path),
                field=file_name,
            ) from exc

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise RuleConfigError(
                f"规则文件根节点必须是字典（映射），当前为 {type(data).__name__}",
                path=str(path),
                field=file_name,
            )
        return data

    @staticmethod
    def _as_str_tuple(value: Any) -> tuple[str, ...]:
        """把 YAML 值规范化为字符串元组（忽略 ``None``，逐项 str）。"""
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple("" if item is None else str(item) for item in value)
        return (str(value),)

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """把 YAML 值规范化为 int。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """把 YAML 值规范化为 float。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_fields_blacklist(
        self, data: dict[str, Any], source: str
    ) -> FieldBlacklistRules:
        """构造字段名黑名单快照。"""
        return FieldBlacklistRules(
            terms=self._as_str_tuple(data.get("terms")),
            skip_prefixes=self._as_str_tuple(data.get("skip_prefixes")),
            source_path=source,
        )

    def _build_brand_patterns(self, data: dict[str, Any], source: str) -> BrandPatternRules:
        """构造品牌正则快照。

        Raises:
            RuleConfigError: ``patterns`` 条目缺少 ``regex`` 字段。
        """
        raw_patterns = data.get("patterns") or []
        if not isinstance(raw_patterns, (list, tuple)):
            raise RuleConfigError(
                "brand_patterns.patterns 必须是列表",
                path=source,
                field="patterns",
            )

        patterns: list[tuple[str, str, str]] = []
        for idx, item in enumerate(raw_patterns):
            if not isinstance(item, dict) or "regex" not in item:
                raise RuleConfigError(
                    f"patterns[{idx}] 缺少必填字段 regex",
                    path=source,
                    field=f"patterns[{idx}].regex",
                )
            name = str(item.get("name", f"pattern_{idx}"))
            regex = str(item.get("regex", ""))
            note = str(item.get("note", ""))
            patterns.append((name, regex, note))

        return BrandPatternRules(
            patterns=tuple(patterns),
            none_tokens=self._as_str_tuple(data.get("none_tokens")),
            source_path=source,
        )

    def _build_model_clean(self, data: dict[str, Any], source: str) -> ModelCleanRules:
        """构造型号清洗规则快照。"""
        anchor = data.get("anchor_regex") or r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{1,39}"
        return ModelCleanRules(
            anchor_regex=str(anchor),
            dirty_tail_chars=self._as_str_tuple(data.get("dirty_tail_chars")),
            protect_patterns=self._as_str_tuple(data.get("protect_patterns")),
            model_field_names=self._as_str_tuple(data.get("model_field_names")),
            source_path=source,
        )

    def _build_whole_machine(
        self, data: dict[str, Any], source: str
    ) -> WholeMachineBrandRules:
        """构造外箱整机品牌上下文规则快照。

        ``context_scope`` 缺省 ``whole_image``（缺陷 B：整图全文检索）。
        """
        return WholeMachineBrandRules(
            context_tokens=self._as_str_tuple(data.get("context_tokens")),
            context_window=self._as_int(data.get("context_window"), 3),
            context_scope=str(data.get("context_scope", "whole_image") or "whole_image"),
            verdict_hint=str(data.get("verdict_hint", "") or ""),
            source_path=source,
        )

    def _build_non_brand_tokens(
        self, data: dict[str, Any], source: str
    ) -> NonBrandTokenRules:
        """构造非品牌裸 token 规则快照（缺陷 C）。"""
        return NonBrandTokenRules(
            tokens=self._as_str_tuple(data.get("tokens")),
            source_path=source,
        )

    def _build_ocr_kv_patterns(
        self, data: dict[str, Any], source: str
    ) -> OcrKvPatternRules:
        """构造 OCR 键值对（KV）提取规则快照（v0.2.0 点 8）。

        ``field_aliases`` 允许两种 YAML 形态：
          * **映射**（推荐）：``{字段名: [别名, ...]}``；
          * **列表**：``[{name: ..., aliases: [...]}, ...]``。

        Raises:
            RuleConfigError: ``field_aliases`` 结构非法（既非映射也非列表）。
        """
        raw_aliases = data.get("field_aliases")
        aliases: list[tuple[str, tuple[str, ...]]] = []

        if raw_aliases is None:
            aliases = []
        elif isinstance(raw_aliases, dict):
            for name, tokens in raw_aliases.items():
                aliases.append((str(name), self._as_str_tuple(tokens)))
        elif isinstance(raw_aliases, (list, tuple)):
            for idx, item in enumerate(raw_aliases):
                if not isinstance(item, dict) or "name" not in item:
                    raise RuleConfigError(
                        f"field_aliases[{idx}] 缺少必填字段 name",
                        path=source,
                        field=f"field_aliases[{idx}]",
                    )
                name = str(item.get("name", ""))
                aliases.append((name, self._as_str_tuple(item.get("aliases"))))
        else:
            raise RuleConfigError(
                "ocr_kv_patterns.field_aliases 必须是映射或列表",
                path=source,
                field="field_aliases",
            )

        allow_next = data.get("allow_next_line_value")
        return OcrKvPatternRules(
            field_aliases=tuple(aliases),
            separators=self._as_str_tuple(data.get("separators")),
            whitespace_separator_min=self._as_int(
                data.get("whitespace_separator_min"), 2
            ),
            noise_terms=self._as_str_tuple(data.get("noise_terms")),
            allow_next_line_value=bool(allow_next) if allow_next is not None else False,
            source_path=source,
        )

    def _build_separators(self, data: dict[str, Any], source: str) -> SeparatorRules:
        """构造分隔符归一化规则快照。"""
        return SeparatorRules(
            separators=self._as_str_tuple(data.get("separators")),
            canonical_separator=str(data.get("canonical_separator", "|")),
            colon_variants=self._as_str_tuple(data.get("colon_variants")),
            canonical_colon=str(data.get("canonical_colon", ":")),
            invisible_chars=self._as_str_tuple(data.get("invisible_chars")),
            source_path=source,
        )

    def _build_noise_signals(self, data: dict[str, Any], source: str) -> NoiseSignalRules:
        """构造噪声信号规则快照（含 v1.2 易混字符 + 模糊相似度）。

        Raises:
            RuleConfigError: ``confusable_chars`` 结构非法（非列表的列表）。
        """
        raw_confusable = data.get("confusable_chars") or []
        if not isinstance(raw_confusable, (list, tuple)):
            raise RuleConfigError(
                "noise_signals.confusable_chars 必须是列表的列表",
                path=source,
                field="confusable_chars",
            )
        confusable: list[tuple[str, ...]] = []
        for idx, group in enumerate(raw_confusable):
            if not isinstance(group, (list, tuple)) or not group:
                raise RuleConfigError(
                    f"confusable_chars[{idx}] 必须是非空列表",
                    path=source,
                    field=f"confusable_chars[{idx}]",
                )
            confusable.append(tuple(str(ch) for ch in group))

        fuzzy = data.get("fuzzy_similarity") or {}
        if not isinstance(fuzzy, dict):
            raise RuleConfigError(
                "noise_signals.fuzzy_similarity 必须是字典",
                path=source,
                field="fuzzy_similarity",
            )

        raw_samples = data.get("known_noise_samples") or []
        samples: list[dict[str, str]] = []
        if isinstance(raw_samples, (list, tuple)):
            for item in raw_samples:
                if isinstance(item, dict):
                    samples.append({str(k): str(v) for k, v in item.items()})

        return NoiseSignalRules(
            confusable_chars=tuple(confusable),
            fuzzy_max_edit_distance=self._as_int(
                fuzzy.get("max_edit_distance"), 2
            ),
            fuzzy_min_length=self._as_int(fuzzy.get("min_length"), 5),
            fuzzy_verdict=str(fuzzy.get("verdict", "SUSPICIOUS") or "SUSPICIOUS"),
            low_confidence_threshold=self._as_float(
                data.get("low_confidence_threshold"), 0.5
            ),
            fragment_min_chars=self._as_int(data.get("fragment_min_chars"), 2),
            known_noise_samples=tuple(samples),
            source_path=source,
        )
