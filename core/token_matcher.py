"""完整分词匹配器（core.token_matcher，v0.3.0 需求 1）。

**唯一职责**：判断「申报要素的**值**」是否在**某张图的 OCR 全文**里以
**完整分词**出现。这是 v0.3.0 判定主链路的**唯一取证入口**——取代旧链路的
「四层正则先猜出图片侧值，再比对」（``extract_detected_brand`` /
``extract_detected_model`` 的**取证**职能）。

## 为什么改（口径变更，2026-09-16 用户拍板）

旧链路要求「先猜对图片侧的字段值」才能比对，而 OCR **缺失空间感知**（无左右/上下
关系）→ 字段名与值分行、值被相邻字段名抢走 → 猜错或猜空 → 误判。
新链路把「猜」这一步整段拆掉：**申报值出现在 OCR 全文的完整分词里 → 命中**。

## 三级匹配

    ============  ==========================================================
    级别          触发与实现
    ============  ==========================================================
    ``EXACT``     英文/数字按**词边界**（``JYB`` 命中 ``品牌:JYB`` / ``JYB-100``，
                  **不命中** ``JYBA01``）；中文按**子串**（``宇同`` 命中 ``宇同电子``）
    ``FUZZY``     OCR 误读容错：① ``known_noise_samples`` 已知误读映射纠正后重跑
                  （纠正目标本身是字段名时**跳过**，见下）；② 易混字符等价类
                  （``O↔0`` / ``I↔1↔l`` / ``W↔H`` / ``N↔H`` …）
    ``NONE``      未命中 → 交由 :mod:`core.judge_engine` 做**两级分流**
    ============  ==========================================================

## ⚠️ 必做护栏：字段名上下文剔除（防新链路引入虚高）

中文按子串匹配会带来新的虚高路径：申报品牌 ``创维``，而图上出现
``创维物料编号``（**字段名**）→ 子串命中 → 误判 ✅。

对策（:meth:`TokenMatcher._is_field_name_context`）：命中后检查**右侧紧邻**文本，
若构成字段名（CJK 后缀**紧邻**、ASCII 后缀允许一个空格、或整行归一化后等于
``fields_blacklist`` 词条）→ **该命中作废**。

**缺此护栏 = 直接违反「不虚高」红线。**

## 规则外置

字段名词表 / 已知误读样本 / 易混字符组全部来自注入的
:class:`core.rule_repository.RuleRepository`；``core.constants`` 只作兜底默认值。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from core import constants
from core.models import OcrText
from core.noise_guard import is_none_token
from core.rule_repository import RuleRepository

__all__ = [
    "MATCH_EXACT",
    "MATCH_FUZZY",
    "MATCH_NONE",
    "TokenMatch",
    "TokenMatcher",
]


#: 精确命中（完整分词）
MATCH_EXACT = "EXACT"
#: 模糊命中（OCR 误读已纠正）
MATCH_FUZZY = "FUZZY"
#: 未命中
MATCH_NONE = "NONE"

#: 申报值参与匹配的最小长度（单字符捕获多为残片，与 ``judge_engine._MIN_BRAND_LEN`` 同口径）
_MIN_VALUE_LEN: int = 2

#: 词边界字符集（英文/数字分词）—— ``_`` 亦算词内字符（``JYB_A01`` 不算完整分词）
_WORD_CHAR_CLASS = "A-Za-z0-9_"

#: 命中行文本截断长度（供 UI 展示 / JSON 追溯）
_SAMPLE_LINE_LIMIT: int = 120

#: 字段名护栏：右侧扫描窗口（字符数）
_GUARD_WINDOW: int = 8

#: 字段名后缀提示（中文）—— 必须**紧邻**命中右侧才算字段名语境
_FIELD_SUFFIX_CJK: tuple[str, ...] = (
    "物料编号",
    "编号",
    "代码",
    "名称",
    "全称",
    "地址",
    "日期",
    "数量",
    "原产地",
    "供应商",
    "制造商",
    "生产厂商",
    "批次",
)

#: 字段名后缀提示（英文/数字）—— 允许一个空格，须**词边界**匹配
#: （防止 ``N011901`` 里的 ``NO`` 被误当字段名后缀）
_FIELD_SUFFIX_ASCII: tuple[str, ...] = (
    "P/N",
    "P/H",
    "PIN",
    "NO",
    "NO.",
    "NUMBER",
    "CODE",
    "NAME",
    "ADDRESS",
    "DATE",
    "QTY",
    "MFR",
    "SUPPLIER",
    "MANUFACTURER",
    "BATCH",
    "LOT",
)

#: 归一化时需要移除的分隔符（与 ``judge_engine._normalize_candidate`` 同口径）
_SEPARATORS_TO_STRIP: tuple[str, ...] = (" ", "\t", "/", "\\", "-", "_", ".", ":", "|")


def _has_cjk(text: str) -> bool:
    """文本是否含中日韩统一表意文字。"""
    return any("\u4e00" <= ch <= "\u9fa5" for ch in text)


def _normalize(text: str) -> str:
    """归一化：大写 + 去空白与常见分隔符（用于字段名词条比对）。"""
    raw = ("" if text is None else str(text)).upper()
    for ch in _SEPARATORS_TO_STRIP:
        raw = raw.replace(ch, "")
    return raw


def _truncate(text: str, limit: int = _SAMPLE_LINE_LIMIT) -> str:
    """截断超长文本（保留尾部省略号）。"""
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


#: 图片文件名中的序号组（如 ``A&001.jpg`` 的 ``001``），用于 ``OcrText.seq`` 缺失时兜底
_IMAGE_SEQ_RE = re.compile(r"[&](?:0*)(\d+)(?=\.[^.]*$|$)")


def _resolve_image_seq(text: Any) -> int:
    """取某张图的序号（优先 ``OcrText.seq``，缺失/为 0 时回退解析文件名 ``&NNN``）。

    ⚠️ 实测（2026-09-16 真跑批快照）：管线产出的 ``OcrText.seq`` 恒为 ``0``
    （序号只落在 ``ImageEvidence.seq`` 上），若只用 ``OcrText.seq``，
    「命中来源图号」会全部退化为 ``0``。故此处按文件名兜底。

    Args:
        text: :class:`core.models.OcrText` 实例。

    Returns:
        图片序号；无法确定时返回 ``0``。
    """
    try:
        seq = int(getattr(text, "seq", 0) or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq > 0:
        return seq
    name = str(getattr(text, "image_path", "") or "")
    match = _IMAGE_SEQ_RE.search(name)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass
class TokenMatch:
    """单字段的完整分词匹配结果（**绝不进 13 列汇总表**，只进 JSON + UI）。

    Attributes:
        field: 字段名（``品牌`` / ``型号``）。
        declared: 申报值原形。
        mode: :data:`MATCH_EXACT` / :data:`MATCH_FUZZY` / :data:`MATCH_NONE`。
        token: 命中的完整分词（``EXACT`` 为图内实际形态；``FUZZY`` 为纠正后形态）。
        images: 命中所在图片序号列表（去重升序；供 UI「命中来源图号」）。
        sample_line: 命中所在 OCR 原文行（截断 120 字，供 UI 展示与追溯）。
        corrected_from: ``FUZZY`` 时图内误读原文（``EXACT`` 为空串）。
        note: 人类可读说明（进判定说明 / 详细 JSON）。
        none_marker: 【口径 v0.3.2】图片中命中到的**显式「无」标记**原文
            （如 ``品牌:无`` / ``无型号``）；未命中为空串。
            ⚠️ 命中标记时 ``mode`` **仍为** :data:`MATCH_NONE`（申报侧为"无"，
            本就不参与分词匹配，"无标记"是另一路证据），判定依据见 ``note``。
        none_marker_image: 显式「无」标记所在图片序号（0 表示未知）。
    """

    field: str = ""
    declared: str = ""
    mode: str = MATCH_NONE
    token: str = ""
    images: list[int] = dc_field(default_factory=list)
    sample_line: str = ""
    corrected_from: str = ""
    note: str = ""
    none_marker: str = ""
    none_marker_image: int = 0

    @property
    def hit(self) -> bool:
        """是否命中（``EXACT`` 或 ``FUZZY``）。"""
        return self.mode in (MATCH_EXACT, MATCH_FUZZY)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供详细 JSON 日志落盘）。"""
        return {
            "field": self.field,
            "declared": self.declared,
            "mode": self.mode,
            "token": self.token,
            "images": list(self.images),
            "sample_line": self.sample_line,
            "corrected_from": self.corrected_from,
            "note": self.note,
            "none_marker": self.none_marker,
            "none_marker_image": self.none_marker_image,
        }


class TokenMatcher:
    """完整分词匹配器（v0.3.0 需求 1）。

    典型用法::

        matcher = TokenMatcher(ocr_texts, rules=repo)
        m = matcher.match("JYB", field="品牌")
        assert m.mode in (MATCH_EXACT, MATCH_FUZZY, MATCH_NONE)

    Args:
        texts: 该记录的 OCR 结果列表（与 ``JudgeEngine.judge`` 的入参同源）。
        rules: 规则仓库；``None`` 时使用 :mod:`core.constants` 兜底默认值。
    """

    def __init__(
        self,
        texts: list[OcrText] | None = None,
        *,
        rules: RuleRepository | None = None,
    ) -> None:
        """构造匹配器（规则一次性解析为不可变快照，不做热加载）。"""
        self._texts: list[OcrText] = [t for t in (texts or []) if t is not None]
        self._rules: RuleRepository | None = rules
        self._field_terms: frozenset[str] = self._load_field_terms()
        self._known_samples: tuple[dict[str, str], ...] = self._load_known_samples()
        self._confusable_map: dict[str, str] = self._load_confusable_map()

    # ══════════════════════════════════════════════════════════
    #  规则解析（外置优先，constants 兜底）
    # ══════════════════════════════════════════════════════════

    def _load_field_terms(self) -> frozenset[str]:
        """加载字段名词表（归一化形态），用于护栏 G2。"""
        terms: tuple[str, ...] = ()
        if self._rules is not None:
            try:
                loaded = self._rules.get().fields_blacklist.terms
                if loaded:
                    terms = tuple(loaded)
            except Exception:  # noqa: BLE001 - 规则加载失败一律退兜底，保证不崩
                terms = ()
        if not terms:
            terms = tuple(constants.DEFAULT_FIELD_BLACKLIST)
        return frozenset(_normalize(t) for t in terms if _normalize(t))

    def _load_known_samples(self) -> tuple[dict[str, str], ...]:
        """加载已知 OCR 误读样本表（``noise_signals.yaml::known_noise_samples``）。"""
        if self._rules is None:
            return ()
        try:
            samples = self._rules.get().noise_signals.known_noise_samples
        except Exception:  # noqa: BLE001
            return ()
        return tuple(s for s in (samples or ()) if isinstance(s, dict))

    def _load_confusable_map(self) -> dict[str, str]:
        """加载易混字符等价类 → ``{字符: 等价字符类（正则字符类内容）}``。"""
        groups: Any = ()
        if self._rules is not None:
            try:
                loaded = self._rules.get().noise_signals.confusable_chars
                if loaded:
                    groups = loaded
            except Exception:  # noqa: BLE001
                groups = ()
        if not groups:
            groups = constants.DEFAULT_CONFUSABLE_CHARS

        mapping: dict[str, set[str]] = {}
        for group in groups or ():
            chars = [str(c).upper() for c in (group or ()) if str(c)]
            for ch in chars:
                slot = mapping.setdefault(ch, set())
                slot.update(chars)
        return {
            ch: "".join(sorted(chars))
            for ch, chars in mapping.items()
            if chars
        }

    # ══════════════════════════════════════════════════════════
    #  公共入口
    # ══════════════════════════════════════════════════════════

    @property
    def texts(self) -> list[OcrText]:
        """本次匹配使用的 OCR 结果列表（只读视图）。"""
        return list(self._texts)

    def match(self, declared: str, *, field: str = "") -> TokenMatch:
        """判断申报值是否在 OCR 全文里以**完整分词**出现。

        Args:
            declared: 申报值（品牌 / 型号）。
            field: 字段名（``品牌`` / ``型号``，仅用于文案）。

        Returns:
            :class:`TokenMatch`（``mode`` 为 ``EXACT`` / ``FUZZY`` / ``NONE``）。
        """
        value = ("" if declared is None else str(declared)).strip()
        label = field or "字段"

        if not value:
            return TokenMatch(
                field=field,
                declared=value,
                mode=MATCH_NONE,
                note=f"{label}：申报值为空，未参与完整分词匹配",
            )
        if is_none_token(value):
            return TokenMatch(
                field=field,
                declared=value,
                mode=MATCH_NONE,
                note=f"{label}：申报值为『{value}』（视为无），未参与完整分词匹配",
            )
        if len(value) < _MIN_VALUE_LEN:
            return TokenMatch(
                field=field,
                declared=value,
                mode=MATCH_NONE,
                note=f"{label}：申报值『{value}』长度 < {_MIN_VALUE_LEN}，不参与完整分词匹配",
            )

        exact = self._search_exact(value, field)
        if exact is not None:
            return exact

        fuzzy = self._search_fuzzy(value, field)
        if fuzzy is not None:
            return fuzzy

        return TokenMatch(
            field=field,
            declared=value,
            mode=MATCH_NONE,
            note=f"{label}：图片 OCR 全文未出现完整分词『{value}』",
        )

    # ══════════════════════════════════════════════════════════
    #  ① EXACT —— 完整分词精确命中
    # ══════════════════════════════════════════════════════════

    def _search_exact(self, value: str, field: str) -> TokenMatch | None:
        """EXACT 级检索（英文/数字词边界，中文子串），含字段名护栏。"""
        hits: list[tuple[int, str, str]] = []
        voided = 0
        for seq, line in self._iter_lines():
            for start, end, matched in self._iter_exact_hits(line, value):
                if self._is_field_name_context(line, start, end):
                    voided += 1
                    continue
                hits.append((seq, line, matched))
                break

        if not hits:
            if voided:
                # 命中过但全被护栏作废 → 明确说明（可追溯，非静默丢弃）
                return TokenMatch(
                    field=field,
                    declared=value,
                    mode=MATCH_NONE,
                    note=(
                        f"{field or '字段'}：『{value}』仅出现在字段名语境"
                        f"（如『创维物料编号』），不构成值证据，命中作废"
                    ),
                )
            return None

        images = sorted({seq for seq, _, _ in hits})
        seq, line, matched = hits[0]
        return TokenMatch(
            field=field,
            declared=value,
            mode=MATCH_EXACT,
            token=matched,
            images=images,
            sample_line=_truncate(line),
            note=(
                f"{field or '字段'}：图片中以完整分词命中『{matched}』"
                f"（图 {_format_images(images)}）"
            ),
        )

    def _iter_exact_hits(self, line: str, value: str) -> Iterator[tuple[int, int, str]]:
        """在某行内枚举申报值的全部精确命中（``(start, end, matched)``）。"""
        if _has_cjk(value):
            start = line.find(value)
            while start != -1:
                yield start, start + len(value), value
                start = line.find(value, start + 1)
            return

        pattern = re.compile(
            rf"(?<![{_WORD_CHAR_CLASS}]){re.escape(value)}(?![{_WORD_CHAR_CLASS}])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(line):
            yield match.start(), match.end(), match.group(0)

    # ══════════════════════════════════════════════════════════
    #  ② FUZZY —— OCR 误读容错
    # ══════════════════════════════════════════════════════════

    def _search_fuzzy(self, value: str, field: str) -> TokenMatch | None:
        """FUZZY 级检索：① 已知误读样本纠正 → ② 易混字符等价类。"""
        known = self._search_known_sample(value, field)
        if known is not None:
            return known
        return self._search_confusable(value, field)

    def _search_known_sample(self, value: str, field: str) -> TokenMatch | None:
        """① 命中 ``known_noise_samples`` → 按 ``expected_actual`` 纠正后重跑 EXACT。

        ⚠️ **同源护栏（与 G2 同一原则）**：若样本的纠正目标 ``expected_actual``
        **本身就是字段名**（如 ``SKYWORTH P/N`` = ``创维物料编号`` 的英文字段名，
        已在 ``fields_blacklist`` 词表内），则纠正后仍是字段名 → **不构成"值"证据**
        → 该样本直接跳过。缺此规则会让「申报品牌 ``SKYWORTH``」仅凭图上
        ``SKYWORTH P/N`` 这个**字段名**被判命中（虚高）。
        """
        for seq, line in self._iter_lines():
            for sample in self._known_samples:
                misread = self._match_misread_form(sample, line)
                if not misread:
                    continue
                expected = str(sample.get("expected_actual", "") or "").strip()
                if not expected:
                    continue
                if self._is_field_name_value(expected):
                    continue
                if not self._value_in_text(expected, value):
                    continue
                return TokenMatch(
                    field=field,
                    declared=value,
                    mode=MATCH_FUZZY,
                    token=expected,
                    images=[seq],
                    sample_line=_truncate(line),
                    corrected_from=misread,
                    note=(
                        f"{field or '字段'}：图内『{misread}』为已知 OCR 误读，"
                        f"已纠正为『{expected}』"
                    ),
                )
        return None

    @staticmethod
    def _match_misread_form(sample: dict[str, str], line: str) -> str:
        """判断某行是否含样本的误读形态（整行 ``ocr`` 或前缀 ``ocr_prefix``）。

        Returns:
            命中的误读形态字符串；未命中返回空串。
        """
        upper_line = line.upper()
        for key in ("ocr", "ocr_prefix"):
            form = str(sample.get(key, "") or "").strip()
            if not form:
                continue
            if len(form) >= 3 and form.upper() in upper_line:
                return form
            if _normalize(form) and _normalize(form) == _normalize(line):
                return form
        return ""

    def _search_confusable(self, value: str, field: str) -> TokenMatch | None:
        """② 易混字符等价类命中（``SKYWORTH`` ↔ ``SKYHORTH``）。

        ⚠️ 与 EXACT 同样受字段名护栏约束（防止把 ``SKYHORTH P/H`` 这类
        **字段名**误读当作值证据）。
        """
        pattern = self._confusable_pattern(value)
        if pattern is None:
            return None
        for seq, line in self._iter_lines():
            for match in pattern.finditer(line):
                start, end = match.start(), match.end()
                if self._is_field_name_context(line, start, end):
                    continue
                return TokenMatch(
                    field=field,
                    declared=value,
                    mode=MATCH_FUZZY,
                    token=value,
                    images=[seq],
                    sample_line=_truncate(line),
                    corrected_from=match.group(0),
                    note=(
                        f"{field or '字段'}：图内『{match.group(0)}』与申报『{value}』"
                        "仅易混字符不同（OCR 疑似误读），已纠正后命中"
                    ),
                )
        return None

    def _confusable_pattern(self, value: str) -> re.Pattern[str] | None:
        """按易混字符等价类构造申报值的匹配正则（无任何等价类可用时返回 ``None``）。"""
        parts: list[str] = []
        changed = False
        for ch in value:
            upper = ch.upper()
            group = self._confusable_map.get(upper, "")
            if len(group) > 1:
                changed = True
                parts.append(f"[{re.escape(group)}]")
            else:
                parts.append(re.escape(ch))
        if not changed:
            return None
        body = "".join(parts)
        return re.compile(
            rf"(?<![{_WORD_CHAR_CLASS}]){body}(?![{_WORD_CHAR_CLASS}])",
            re.IGNORECASE,
        )

    @staticmethod
    def _value_in_text(text: str, value: str) -> bool:
        """判断 ``value`` 是否以**完整分词**出现在 ``text`` 中（供 tier-a 复用）。"""
        if not text or not value:
            return False
        if _has_cjk(value):
            return value in text
        pattern = re.compile(
            rf"(?<![{_WORD_CHAR_CLASS}]){re.escape(value)}(?![{_WORD_CHAR_CLASS}])",
            re.IGNORECASE,
        )
        return pattern.search(text) is not None

    # ══════════════════════════════════════════════════════════
    #  护栏：字段名上下文剔除（防虚高）
    # ══════════════════════════════════════════════════════════

    def _is_field_name_value(self, text: str) -> bool:
        """判断整串文本是否**本身就是字段名**（归一化后命中字段名词表）。

        用于 tier-a（已知误读纠正目标）的同源护栏。

        Args:
            text: 待判定文本（如 ``SKYWORTH P/N``）。

        Returns:
            ``True`` 表示该串是字段名，不可作为"值"证据。
        """
        normalized = _normalize(text)
        return bool(normalized) and normalized in self._field_terms

    def _is_field_name_context(self, line: str, start: int, end: int) -> bool:
        """判断某次命中是否处于**字段名语境**（是 → 该次命中作废）。

        判据（任一成立即作废）：

          * **G1-CJK**：命中右侧**紧邻**位置即为中文字段名后缀
            （``创维`` + ``物料编号`` → ``创维物料编号``）；
          * **G1-ASCII**：命中右侧（跳过空白）以英文/数字字段名后缀**词边界**开头
            （``SKYWORTH`` + `` P/N``、``Manufacturer`` + `` Name``）；
          * **G2**：从命中位置向右延展的连续文本归一化后等于
            ``fields_blacklist`` 词条（``SKYWORTH P/N`` / ``MFR P/N`` 等）。

        Args:
            line: 命中所在 OCR 行。
            start: 命中起始下标。
            end: 命中结束下标（不含）。

        Returns:
            ``True`` 表示字段名语境（命中应作废）。
        """
        # ── G1-CJK：紧邻中文字段名后缀 ──
        for suffix in _FIELD_SUFFIX_CJK:
            if line.startswith(suffix, end):
                return True

        # ── G1-ASCII：右侧跳空白后的词边界字段名后缀 ──
        right = line[end : end + _GUARD_WINDOW]
        stripped = right.lstrip(" \t")
        for suffix in _FIELD_SUFFIX_ASCII:
            if re.match(
                rf"{re.escape(suffix)}(?![{_WORD_CHAR_CLASS}])",
                stripped,
                re.IGNORECASE,
            ):
                return True

        # ── G2：右侧延展串归一化后命中字段名词表 ──
        if self._field_terms:
            extended = self._extend_right(line, start)
            if extended and _normalize(extended) in self._field_terms:
                return True
        return False

    @staticmethod
    def _extend_right(line: str, start: int) -> str:
        """从 ``start`` 起向右延展连续文本（字母/数字/常见分隔符/中文）用于 G2 比对。"""
        index = start
        allowed = set(" \t/-_.:")
        while index < len(line):
            ch = line[index]
            if ch.isalnum() or ch in allowed or _has_cjk(ch):
                index += 1
                continue
            break
        return line[start:index].strip()

    # ══════════════════════════════════════════════════════════
    #  内部工具
    # ══════════════════════════════════════════════════════════

    def _iter_lines(self) -> Iterator[tuple[int, str]]:
        """枚举全部图的非空行（``(图片序号, 行文本)``，**逐图**，绝不跨图拼接）。"""
        for text in self._texts:
            raw = getattr(text, "text_raw", "") or ""
            if not raw:
                continue
            seq = _resolve_image_seq(text)
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped:
                    yield seq, stripped


def _format_images(images: list[int]) -> str:
    """把图片序号列表格式化为人类可读串。"""
    return "、".join(str(i) for i in images) if images else "—"
