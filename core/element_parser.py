"""申报要素解析（core.element_parser，对应架构设计 4 节类图 + 6.1/6.3 + SOP 3.1/3.5）。

**三段式解析管线**（架构设计 C3）：:

    ① 不可见字符剥离  _strip_invisible        （SOP 3.5 规则#4：零宽字符污染）
    ② 分隔符归一化    _normalize_separators  （P8：| / 、 / ; 混用，: / ： 混用）
    ③ 多形态正则 + 黑名单护栏  _extract_brand / _extract_model / _apply_blacklist

**规则外置（v1.1 C + v1.2 13.5.2）**：本模块**禁止硬编码正则或黑名单**，
所有规则从注入的 :class:`core.rule_repository.RuleRepository` 读取：:

    rules/brand_patterns.yaml    —— 品牌正则形态 + none_tokens
    rules/model_clean_rules.yaml —— 型号头部锚定 + 脏尾字符集 + 保护模式
    rules/fields_blacklist.yaml  —— 字段名黑名单 + 语境排除前缀
    rules/separators.yaml        —— 分隔符/冒号归一化表 + 不可见字符
    rules/noise_signals.yaml     —— 易混字符表（供模糊比对复用）

**契约（架构设计 4 节接口表）**：:meth:`ElementParser.parse` **永不抛异常**，
最坏返回空 ``brand`` / ``model``，由 ``JudgeEngine`` 判定"缺图内标识"。

依赖：``core.models`` / ``core.constants`` / ``core.rule_repository`` / ``infra.*``
（**不依赖 PySide6**）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from core import constants as C
from core.models import ParsedElement
from core.rule_repository import RuleRepository, RuleSet
from infra.logger import Phase, get_logger

__all__ = [
    "ElementParser",
    "strip_invisible",
    "normalize_separators",
    "clean_model_tail",
    "is_blacklisted",
    "split_fields",
]

_logger = get_logger(Phase.PHASE2)

#: 字段分隔符归一化后的内部键值对分隔符（半角冒号）
_INTERNAL_COLON = ":"

#: 纯 token 品牌兜底正则：ASCII（字母/数字/连字符/点）或 2+ 位中文，长度 1–30。
#: 说明：当品牌值已与键分离（``品牌:baori`` → ``baori``）且不匹配任何品牌正则形态时，
#: 用该正则确认"值是一个干净的 token"后采信，仍经黑名单与语境护栏二次过滤。
_BRAND_TOKEN_RE: re.Pattern[str] = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9\-\.]{0,29}|[\u4e00-\u9fa5]{2,30})$"
)

#: 型号清洗的脏尾分界正则缓存键（按脏尾字符集哈希，避免重复编译）
_TAIL_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}

#: 语境排除前缀（``适用于``/``用于``）的匹配窗口长度（缺陷 E 裁决，N=8）：
#: 仅当候选词**之前** N 字符内出现该前缀才判空；SOP 陷阱 #5 语义为"捕获词前"。
#: ``适用于`` 后通常紧跟品牌名（``适用于DAEWOO``），8 字符足以覆盖"紧邻"场景，
#: 又能避免跨句误伤（如型号值尾部 ``…适用于DAEWOO牌电视机`` 与型号头相距 > 8 字符）。
_SKIP_PREFIX_WINDOW: int = 8


# ══════════════════════════════════════════════════════════════════
#  模块级纯函数（供单测直接调用；不含状态）
# ══════════════════════════════════════════════════════════════════


def strip_invisible(text: str, invisible_chars: Sequence[str] | None = None) -> str:
    """剥离不可见字符（SOP 3.5 规则#4：零宽字符污染）。

    说明：``\\s`` **不匹配** ``\\u200b``（ZERO WIDTH SPACE），故必须显式剥离。

    Args:
        text: 原始文本。
        invisible_chars: 待剥离字符集；缺省用 ``rules/separators.yaml`` 的
            ``invisible_chars``，再缺省用 :data:`core.constants.DEFAULT_SEPARATORS`
            的兜底不可见字符集。

    Returns:
        剥离后的文本（``text`` 为 ``None`` 时返回 ``""``）。
    """
    if not text:
        return ""
    chars = invisible_chars
    if chars is None:
        chars = _DEFAULT_INVISIBLE
    result = str(text)
    for ch in chars:
        if ch:
            result = result.replace(ch, "")
    return result


#: 默认不可见字符集（rules/separators.yaml 缺失时的兜底，架构设计 12.C.4）
_DEFAULT_INVISIBLE: tuple[str, ...] = (
    "\u200b",
    "\u200c",
    "\u200d",
    "\ufeff",
    "\u00a0",
    "\u3000",
)


def normalize_separators(
    text: str,
    separators: Sequence[str] | None = None,
    colon_variants: Sequence[str] | None = None,
    canonical_separator: str = "|",
    canonical_colon: str = ":",
) -> str:
    """分隔符 + 冒号归一化（P8：``|`` / ``、`` / ``;`` 混用，``:`` / ``：`` 混用）。

    Args:
        text: 待归一化文本。
        separators: 需归一化为 ``canonical_separator`` 的分隔符集。
        colon_variants: 需归一化为 ``canonical_colon`` 的冒号变体集。
        canonical_separator: 归一化目标分隔符。
        canonical_colon: 归一化目标冒号。

    Returns:
        归一化后的文本。
    """
    if not text:
        return ""
    seps = list(separators) if separators is not None else list(C.DEFAULT_SEPARATORS)
    colons = (
        list(colon_variants)
        if colon_variants is not None
        else list(C.DEFAULT_COLON_VARIANTS)
    )
    result = str(text)
    for sep in seps:
        if sep and sep != canonical_separator:
            result = result.replace(sep, canonical_separator)
    for colon in colons:
        if colon and colon != canonical_colon:
            result = result.replace(colon, canonical_colon)
    return result


def split_fields(text: str) -> list[tuple[str, str]]:
    """把归一化后的申报要素文本拆成 ``(键, 值)`` 对。

    **容错**：无冒号的段落按"单 token"处理（键 = 原文、值 = 原文），
    以便品牌形态 ``无品牌`` / ``宇同品牌`` / ``SAMSUNG牌`` 能被识别。

    Args:
        text: 已归一化的文本（分隔符为 ``|``、冒号为 ``:``）。

    Returns:
        ``[(key, value), ...]``，顺序与原文一致；空段落跳过。
    """
    pairs: list[tuple[str, str]] = []
    for chunk in (text or "").split("|"):
        part = chunk.strip().strip(";").strip()
        if not part:
            continue
        if _INTERNAL_COLON in part:
            key, _, value = part.partition(_INTERNAL_COLON)
            pairs.append((key.strip(), value.strip()))
        else:
            pairs.append((part, part))
    return pairs


def clean_model_tail(
    value: str,
    dirty_tail_chars: Sequence[str] | None = None,
    protect_patterns: Sequence[str] | None = None,
    anchor_regex: str | None = None,
) -> str:
    """型号脏尾清洗（SOP 3.5 规则#7，**头部锚定首个 ASCII 型号主体**）。

    核心约束（rules/model_clean_rules.yaml 注释）：**绝不可**用贪婪 ``.*`` 截断，
    否则会误伤 ``2.402GHz``（被截成 ``2.402``）。

    算法：
      1. 若整体命中 ``protect_patterns``（``2.402GHz`` / ``300W`` 等）→ 原样返回；
      2. 用 ``anchor_regex`` 从字符串**首部**锚定型号主体；
      3. 在锚定结果内，于**首个脏尾字符**处截断（空格也计脏尾，故
         ``A7A01G ，电视机用/`` → ``A7A01G``）。

    Args:
        value: 型号原值。
        dirty_tail_chars: 脏尾字符集。
        protect_patterns: 保护模式正则（命中则原样返回）。
        anchor_regex: 头部锚定正则。

    Returns:
        清洗后的型号（``""`` 表示无）。
    """
    raw = (value or "").strip()
    if not raw:
        return ""

    tails = (
        tuple(dirty_tail_chars)
        if dirty_tail_chars is not None
        else tuple(C.DEFAULT_DIRTY_TAIL_CHARS)
    )
    protects = (
        list(protect_patterns)
        if protect_patterns is not None
        else _DEFAULT_PROTECT_PATTERNS
    )
    anchor = anchor_regex or _DEFAULT_ANCHOR_REGEX

    # ① 保护模式：整体命中则原样返回（防误伤 2.402GHz / 300W）
    for pattern in protects:
        if re.match(pattern, raw):
            return raw

    # ② 头部锚定首个 ASCII 型号主体
    match = re.match(anchor, raw)
    if match is None:
        # 无法锚定（如纯中文）→ 返回原文（由后续护栏判定）
        return raw
    body = match.group(0)

    # ③ 在锚定结果内于首个脏尾字符处截断
    cut = len(body)
    for tail in tails:
        if not tail:
            continue
        pos = body.find(tail)
        if pos != -1 and pos < cut:
            cut = pos
    cleaned = body[:cut].strip()

    # 保护模式二次确认（截断后仍命中则还原，避免边界误伤）
    for pattern in protects:
        if re.match(pattern, cleaned):
            return cleaned

    return cleaned


#: 默认锚定正则（rules/model_clean_rules.yaml 缺失时的兜底）
_DEFAULT_ANCHOR_REGEX: str = r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{1,39}"

#: 默认保护模式（同上，兜底）
_DEFAULT_PROTECT_PATTERNS: tuple[str, ...] = (
    r"^\d+(?:\.\d+)?(?:GHz|MHz|KHz|Hz|W|KW|V|A|mA|mm|cm|kg|g)$",
    r"^\d+\.\d+[A-Za-z]*$",
)


def is_blacklisted(
    value: str,
    terms: Sequence[str] | None = None,
    skip_prefixes: Sequence[str] | None = None,
    context: str = "",
) -> bool:
    """判断某个候选值是否命中字段名黑名单 / 语境排除前缀（SOP 3.5 规则#1/#3/#5）。

    **缺陷 E 裁决（架构裁决文档 5A.2）**：``skip_prefixes`` 的匹配由"全串包含"
    改为"**捕获词前 N 字符窗口**"——语义严格对齐 SOP 陷阱 #5 原文：

        捕获词**前**若有 ``适用于`` / ``用于`` / ``适用`` / ``适配``，须跳过
        —— 那是**适配对象**不是申报品牌。

    ⚠️ 旧实现 ``prefix in haystack`` 是**全串包含**匹配，会把"候选值**尾部**出现的
    ``适用于DAEWOO牌电视机``"也当命中，导致型号 ``HS-8A50J-12``（尾部含"适用于"）
    被误判为空（实测 seq=16/17/18 型号丢失）。改为**前缀窗口**后，仅当 ``适用于``
    紧邻出现在**捕获词之前** ``_SKIP_PREFIX_WINDOW`` 个字符内才跳过（``适用于``
    后通常紧跟品牌名 → 窗口足以覆盖"紧邻"场景；跨句的远距离出现不再误伤）。

    Args:
        value: 候选值（捕获词）。
        terms: 字段名黑名单。
        skip_prefixes: 语境排除前缀（**仅**在 ``value`` 之前的前缀窗口内匹配）。
        context: 该候选值的上下文（如整段原文），用于定位捕获词前的窗口。

    Returns:
        ``True`` 表示应判为空。
    """
    candidate = (value or "").strip()
    if not candidate:
        return True

    blacklist = list(terms) if terms is not None else list(C.DEFAULT_FIELD_BLACKLIST)
    # 字段名黑名单：候选值含黑名单词（归一化后比较，忽略大小写与冒号）
    norm_candidate = re.sub(r"\s+", "", candidate).upper().rstrip(":：")
    for term in blacklist:
        if not term:
            continue
        norm_term = re.sub(r"\s+", "", str(term)).upper().rstrip(":：")
        if norm_term and norm_term in norm_candidate:
            return True

    prefixes = (
        list(skip_prefixes) if skip_prefixes is not None else list(C.DEFAULT_SKIP_PREFIXES)
    )
    if not prefixes:
        return False

    # ── 语境排除（缺陷 E：前缀窗口语义，非全串包含）──
    # 定位候选词在 context 中的位置；仅取"其**前** N 字符"窗口，在此窗口内匹配前缀。
    haystack = context or candidate
    pos = haystack.rfind(candidate) if context else 0
    if pos > 0:
        window = haystack[max(0, pos - _SKIP_PREFIX_WINDOW): pos]
    else:
        # 候选不在 context 里（或 context 为空）→ 退回对候选值本身前 N 字符匹配
        window = candidate[: _SKIP_PREFIX_WINDOW] if context else candidate
    if not window:
        return False
    for prefix in prefixes:
        if prefix and prefix in window:
            # 语境排除："适用于/用于"紧邻出现在捕获词**之前** → 判空
            return True

    return False


# ══════════════════════════════════════════════════════════════════
#  解析器
# ══════════════════════════════════════════════════════════════════


class ElementParser:
    """申报要素解析器（SOP 3.1 / 3.5）。

    规则**全部从注入的 :class:`RuleRepository` 读取**，本类不含硬编码正则/黑名单
    （架构设计 12.C.5：``element_parser.py`` 改为从注入的规则对象读取）。

    典型用法::

        repo = RuleRepository()
        repo.load_all()
        parser = ElementParser(repo)
        parsed = parser.parse("品牌:baori|型号:A7A01G ，电视机用/")
        # ParsedElement(brand="baori", model="A7A01G")

    Attributes:
        rules: 当前规则快照（不可变）。
    """

    def __init__(
        self,
        rule_repository: RuleRepository | RuleSet | None = None,
        rules: RuleSet | None = None,
    ) -> None:
        """构造解析器。

        Args:
            rule_repository: 规则仓库（推荐）；会自动 ``get()`` 取快照。
            rules: 直接注入的规则快照（便于单测；与 ``rule_repository`` 二选一）。
        """
        if rules is not None:
            self.rules: RuleSet = rules
        elif rule_repository is not None:
            self.rules = (
                rule_repository.get()
                if isinstance(rule_repository, RuleRepository)
                else rule_repository
            )
        else:
            repo = RuleRepository()
            self.rules = repo.load_all()

        # 预编译品牌正则（规则冻结后不变，编译一次复用）
        self._brand_patterns: list[tuple[str, re.Pattern[str], str]] = (
            self.rules.brand_patterns.compiled()
        )
        self._anchor_pattern: re.Pattern[str] = (
            self.rules.model_clean_rules.compiled_anchor()
        )
        self._protect_patterns: list[str] = list(
            self.rules.model_clean_rules.protect_patterns
        ) or list(_DEFAULT_PROTECT_PATTERNS)
        self._dirty_tail_chars: tuple[str, ...] = tuple(
            self.rules.model_clean_rules.dirty_tail_chars
        ) or tuple(C.DEFAULT_DIRTY_TAIL_CHARS)
        self._model_field_names: list[str] = list(
            self.rules.model_clean_rules.model_field_names
        )
        self._invisible_chars: tuple[str, ...] = tuple(
            self.rules.separators.invisible_chars
        ) or _DEFAULT_INVISIBLE
        self._none_tokens: tuple[str, ...] = tuple(
            self.rules.brand_patterns.none_tokens
        ) or tuple(C.DEFAULT_NONE_TOKENS)

    # ─────────────────── 公开接口 ───────────────────

    def parse(self, raw_text: str) -> ParsedElement:
        """解析申报要素整段原文（**契约：永不抛异常**）。

        最坏情况返回 ``ParsedElement(brand="", model="")``，
        由 ``JudgeEngine`` 判定"缺图内标识"（架构设计 4 节接口表）。

        Args:
            raw_text: 申报要素整段原文（可能含混用分隔符/冒号/零宽字符）。

        Returns:
            :class:`core.models.ParsedElement`。
        """
        result = ParsedElement()
        try:
            cleaned = self._strip_invisible(raw_text or "")
            normalized = self._normalize_separators(cleaned)
            pairs = split_fields(normalized)
            result.fields = pairs

            brand = self._extract_brand(pairs, normalized)
            model = self._extract_model(pairs, normalized)
            result.brand = brand
            result.model = model
        except Exception as exc:  # noqa: BLE001 - 契约要求永不抛异常
            # 记录级异常：仅记 WARN，返回空结果，由 Judge 判"缺图内标识"
            _logger.warning("申报要素解析异常，已降级为空结果：%s", exc)
            result = ParsedElement(notes=[f"解析异常降级：{exc}"])
        return result

    # ─────────────────── 私有管线（类图方法） ───────────────────

    @staticmethod
    def _strip_invisible(text: str) -> str:
        """① 不可见字符剥离（零宽字符等）。"""
        return strip_invisible(text, _DEFAULT_INVISIBLE)

    def _strip_invisible_ex(self, text: str) -> str:
        """① 不可见字符剥离（使用注入规则的字符集）。"""
        return strip_invisible(text, self._invisible_chars)

    def _normalize_separators(self, text: str) -> str:
        """② 分隔符 + 冒号归一化（使用注入规则的表）。"""
        return normalize_separators(
            text,
            separators=self.rules.separators.separators,
            colon_variants=self.rules.separators.colon_variants,
            canonical_separator=self.rules.separators.canonical_separator or "|",
            canonical_colon=self.rules.separators.canonical_colon or ":",
        )

    def _extract_brand(
        self, pairs: Sequence[tuple[str, str]], normalized: str
    ) -> str:
        """③-a 品牌提取（多形态正则 + 黑名单护栏）。

        提取顺序（**先结构化字段，后全文正则兜底**）：
          1. 优先取显式品牌字段（``品牌`` / ``Brand``）的值；
          2. 若值为"无"类 token → 返回空；
          3. 否则对字段值/段落应用品牌正则形态（``宇同品牌`` / ``SAMSUNG牌`` …）；
          4. 全部失败 → 对全文做正则兜底扫描；
          5. 结果经黑名单 + 语境前缀护栏过滤。

        Args:
            pairs: ``(键, 值)`` 对。
            normalized: 归一化后的全文。

        Returns:
            品牌字符串（``""`` 表示无）。
        """
        candidates: list[tuple[str, str]] = []  # (候选值, 上下文)

        for key, value in pairs:
            key_norm = key.strip()
            # 显式品牌字段（键恰为「品牌」/「Brand」）
            if self._is_brand_key(key_norm):
                candidates.append((value, f"{key}:{value}"))

        # 无显式品牌字段时，把含"品牌"字样的段落也纳入候选
        if not candidates:
            for key, value in pairs:
                if "品牌" in key or "品牌" in value:
                    candidates.append((value, f"{key}:{value}"))

        for value, context in candidates:
            brand = self._match_brand_value(value, context)
            if brand:
                return brand

        # 全部候选均被判为"无"→ 明确返回空（**不得**再做全文兜底，否则会把
        # 「无品牌」「品牌:无」里的字样误捕成品牌值）
        if candidates:
            return ""

        # 全文正则兜底（仅当没有任何品牌字段时）
        for _name, pattern, _note in self._brand_patterns:
            for match in pattern.finditer(normalized):
                captured = match.group(1).strip()
                context = normalized[max(0, match.start() - 6): match.end() + 6]
                brand = self._match_brand_value(captured, context)
                if brand:
                    return brand

        return ""

    def _extract_model(
        self, pairs: Sequence[tuple[str, str]], normalized: str
    ) -> str:
        """③-b 型号提取（字段优先 + 脏尾清洗）。

        Args:
            pairs: ``(键, 值)`` 对。
            normalized: 归一化后的全文。

        Returns:
            型号字符串（``""`` 表示无）。
        """
        # ① 显式型号字段（型号 / 规格型号 / Model …）；**键须恰为型号字段名**
        #    （不能用 `in key` —— 否则会把「无型号」当键，误取到别处）
        for key, value in pairs:
            if self._is_model_key(key):
                cleaned = self._extract_model_from_value(value)
                if cleaned:
                    return cleaned

        # ② 键含「型号」但键本身带前缀（如「规格型号」「产品型号」）
        for key, value in pairs:
            if "型号" in key:
                cleaned = self._extract_model_from_value(value)
                if cleaned:
                    return cleaned

        # ③ 值以「型号」开头但无键（如 `无型号` 段落）
        for _key, value in pairs:
            if "型号" in value and _INTERNAL_COLON not in value:
                cleaned = self._extract_model_from_value(value)
                if cleaned:
                    return cleaned

        return ""

    def _extract_model_from_value(self, value: str) -> str:
        """从型号候选值中提取型号（处理「无型号」「型号:无」「A7A01G…」三形态）。

        Args:
            value: 候选值（可能含前缀「型号」字样）。

        Returns:
            清洗后的型号（``""`` 表示无）。
        """
        candidate = (value or "").strip().strip(";").strip()
        if not candidate:
            return ""

        # 形态 A：``无型号`` / ``型号`` 前缀 + 无实际值
        if candidate == "型号" or candidate == "无型号":
            return ""
        # 形态 B：``型号:xxx`` 内嵌（键未分离时）
        if _INTERNAL_COLON in candidate:
            left, _, right = candidate.partition(_INTERNAL_COLON)
            if "型号" in left or left.strip() == "":
                candidate = right.strip()
        # 形态 C：``无型号、用途…`` —— 「无型号」后跟分隔符，说明型号为空
        if candidate.startswith("无型号"):
            return ""
        if candidate.startswith("型号"):
            candidate = candidate[len("型号"):].strip()

        if not candidate:
            return ""
        cleaned = self._clean_model(candidate)
        cleaned = cleaned.strip("、;|,，").strip()
        if not cleaned or self._is_none_token(cleaned):
            return ""
        return cleaned

    def _apply_blacklist(
        self,
        value: str,
        context: str = "",
        skip_prefixes: Sequence[str] | None = None,
    ) -> str:
        """③-c 黑名单护栏：命中则返回空串（SOP 3.5 规则#1/#3/#5）。

        Args:
            value: 候选值。
            context: 语境（供前缀窗口排除）。
            skip_prefixes: 语境排除前缀覆盖值；``None`` 时用规则默认值，
                传 ``()`` 表示**禁用**语境排除（缺陷 E：型号字段不用语境排除）。

        Returns:
            通过护栏的值；被拦截返回 ``""``。
        """
        prefixes = (
            skip_prefixes
            if skip_prefixes is not None
            else self.rules.fields_blacklist.skip_prefixes
        )
        if is_blacklisted(
            value,
            terms=self.rules.fields_blacklist.terms,
            skip_prefixes=prefixes,
            context=context,
        ):
            return ""
        return value

    # ─────────────────── 内部辅助 ───────────────────

    def _is_brand_key(self, key: str) -> bool:
        """判断字段键是否为品牌字段。"""
        norm = re.sub(r"\s+", "", key or "").upper()
        return norm in {"品牌", "BRAND"} or norm.endswith("品牌") or norm.startswith("品牌")

    def _is_model_key(self, key: str) -> bool:
        """判断字段键**恰为**型号字段（用注入的 ``model_field_names``）。

        必须精确匹配：``型号`` / ``规格型号`` / ``产品型号`` / ``Model`` …
        **不得**用 ``in key``（否则「无型号」会被误判为型号字段，
        进而把后续值当作型号）。

        Args:
            key: 字段键。

        Returns:
            ``True`` 表示该键是型号字段名。
        """
        norm = re.sub(r"\s+", "", key or "").upper()
        if not norm:
            return False
        for name in self._model_field_names:
            target = re.sub(r"\s+", "", str(name)).upper()
            if target and norm == target:
                return True
        return norm == "型号"

    def _is_none_token(self, value: str) -> bool:
        """判断值是否属于"无"类 token（SOP 3.4 补充约定）。"""
        norm = re.sub(r"\s+", "", value or "").upper()
        for token in self._none_tokens:
            if re.sub(r"\s+", "", str(token)).upper() == norm:
                return True
        return False

    def _match_brand_value(self, value: str, context: str = "") -> str:
        """对一个候选值应用品牌正则 + 护栏，返回品牌或空串。

        处理顺序：
          1. 候选值含冒号（``品牌:X`` 形态）→ 取冒号右侧，仅对**右侧**做正则/采信；
          2. 候选值不含冒号但以「品牌」结尾（``无品牌`` / ``宇同品牌``）→ 取前缀；
          3. 候选值以「牌」结尾（``SAMSUNG牌``）→ 取前缀；
          4. 否则对候选值本身做品牌正则匹配。

        Args:
            value: 候选值。
            context: 语境（供护栏）。

        Returns:
            品牌字符串（``""`` 表示无）。
        """
        candidate = (value or "").strip().strip(";").strip()
        if not candidate:
            return ""

        # ① 含冒号：只处理冒号右侧（避免把「外观:品牌」类键误当值）
        if _INTERNAL_COLON in candidate:
            _left, _, candidate = candidate.partition(_INTERNAL_COLON)
            candidate = candidate.strip().strip(";").strip()
        elif "品牌" in candidate:
            # ② 以「品牌」结尾的形态：宇同品牌 / 无品牌 → 取前缀为品牌
            prefix = candidate.replace("品牌", "").strip()
            if not prefix:
                return ""
            if self._is_none_token(prefix):
                return ""
            guarded = self._apply_blacklist(prefix, context)
            if guarded:
                return guarded
            return ""
        elif candidate.endswith("牌") and len(candidate) > 1:
            # ③ SAMSUNG牌
            prefix = candidate[:-1].strip()
            if not prefix or self._is_none_token(prefix):
                return ""
            guarded = self._apply_blacklist(prefix, context)
            if guarded:
                return guarded
            return ""

        if not candidate or self._is_none_token(candidate):
            return ""

        # ④ 候选值本身即品牌（键为「品牌」且值已与键分离，如 `品牌:baori` → `baori`）：
        #    经正则形态确认或为纯 ASCII/中文 token（≤ 30 字符、不含字段分隔残余）。
        for _name, pattern, _note in self._brand_patterns:
            match = pattern.search(candidate)
            if match is not None and match.group(1) is not None:
                captured = match.group(1).strip()
                if captured and not self._is_none_token(captured):
                    guarded = self._apply_blacklist(captured, context)
                    if guarded:
                        return guarded

        # ⑤ 纯 token 兜底：值不含「:」且长度合理、非字段名 → 直接采信为品牌
        #    （`baori` 这类小写 ASCII 品牌不在任何正则形态里，但确实是品牌值）
        if _BRAND_TOKEN_RE.match(candidate):
            guarded = self._apply_blacklist(candidate, context)
            if guarded:
                return guarded

        return ""

    def _clean_model(self, value: str) -> str:
        """应用型号清洗（注入规则）+ 黑名单护栏。

        **缺陷 E 裁决（架构裁决文档 5A.2②）**：调用 :meth:`_apply_blacklist` 时
        **``skip_prefixes`` 置空**——`适用于`/`用于` 语境排除的对象是"**申报品牌**"
        （SOP 陷阱 #5），型号字段的脏尾处理已由 :func:`clean_model_tail` +
        ``anchor_regex`` 完成，**不应**因型号值尾部含"适用于X牌"而判空。

        ``terms``（字段名黑名单）**继续对型号生效**：型号若恰为字段名残片仍应判空。
        """
        cleaned = clean_model_tail(
            value,
            dirty_tail_chars=self._dirty_tail_chars,
            protect_patterns=self._protect_patterns,
            anchor_regex=self.rules.model_clean_rules.anchor_regex
            or _DEFAULT_ANCHOR_REGEX,
        )
        # 仅保留 terms 黑名单；skip_prefixes 置空（缺陷 E）
        return self._apply_blacklist(cleaned, value, skip_prefixes=())


def describe_rules(rules: RuleSet) -> dict[str, Any]:
    """返回规则快照摘要（供日志/调试与 T05 菜单展示）。

    Args:
        rules: 规则快照。

    Returns:
        ``{规则名: 条目数}`` 摘要。
    """
    return {
        "brand_patterns": len(rules.brand_patterns.patterns),
        "none_tokens": len(rules.brand_patterns.none_tokens),
        "blacklist_terms": len(rules.fields_blacklist.terms),
        "skip_prefixes": len(rules.fields_blacklist.skip_prefixes),
        "separators": len(rules.separators.separators),
        "invisible_chars": len(rules.separators.invisible_chars),
        "dirty_tail_chars": len(rules.model_clean_rules.dirty_tail_chars),
        "confusable_groups": len(rules.noise_signals.confusable_chars),
    }
