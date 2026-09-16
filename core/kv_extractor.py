"""OCR 行 → 键值对（KV）提取（core.kv_extractor，对应 v0.2.0 方案 §3.4「点 8 的 KV 部分」）。

输入：单张图的 OCR 行（``text`` + ``box`` + ``score``）。
输出：``(kv: dict[str, str], residue: list[str])`` —— 提取到的键值对，以及未能识别为
KV 的**剩余行**。

⚠️ **KV 不参与判定**（Q4 已决）：判定仍走 ``core.judge_engine`` 的原跨图投票链路；
KV 的出口只有「详细 JSON 日志」与「复核工作台展示」两处。

支持的形态（半角/全角冒号、``=``、连续空格弱分隔）::

    Brand:Skyworth          → {"Brand": "Skyworth"}
    品牌：创维               → {"品牌": "创维"}
    MODEL:HS-8AA            → {"MODEL": "HS-8AA"}
    MFR P/N: xxx            → {"MFR P/N": "xxx"}
    Brand  Skyworth         → {"Brand": "Skyworth"}（连续 ≥2 空格，键须命中别名白名单）

**规则外置（C2 原则）**：补规则 = 改 ``rules/ocr_kv_patterns.yaml``，**无需改代码**。
本模块的 :func:`default_kv_rules` 是 **YAML 缺失时的兜底**，与 repo 加载结果**必须等价**
（由 ``tests/test_kv_extractor.py::test_kv_rules_default_and_repo_agree`` 断言，
防"打包漏 YAML → 静默失效"，即历史缺陷 F 的教训）。

**黑名单复用**：字段名黑名单 / 非品牌裸 token **不在此重复维护**，直接复用
``core/constants.DEFAULT_FIELD_BLACKLIST`` / ``DEFAULT_NON_BRAND_TOKENS``
（经 :class:`core.rule_repository.RuleRepository` 可被用户外置覆盖）。

分层：``core`` 只能 import ``core`` / ``infra``，**禁止** import PySide6 / PyQt。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core import constants as C
from core.rule_repository import OcrKvPatternRules, RuleRepository
from infra.logger import Phase, get_logger

__all__ = [
    "OcrLine",
    "KvExtractor",
    "default_kv_rules",
    "default_blacklist",
    "default_non_brand_tokens",
    "extract_kv",
    "KV_FIELD_KEY_RE",
]

#: 「字段键」形态：以字母/数字/CJK 起头，后续允许空格与常见标点（长度另限）。
KV_FIELD_KEY_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9 \u4e00-\u9fff\.\-_/()（）#&]*$"
)

#: 「字段键」最大字符数（超过视为整行正文，不做 KV）。
_KV_FIELD_KEY_MAX_LEN: int = 24

#: 弱分隔（连续空白）正则模板：由 ``whitespace_separator_min`` 渲染。
_WHITESPACE_CLASS = "[ \t\u3000]"


@dataclass
class OcrLine:
    """OCR 单行（``text`` + 可选 ``score`` / ``box``）。"""

    text: str = ""
    score: float = 0.0
    box: Any = None


def _norm(text: str) -> str:
    """归一化键比较用文本（去空白 + 大写）。"""
    return (text or "").strip().upper()


# ══════════════════════════════════════════════════════════════════
#  内置兜底规则（YAML 缺失时的等价默认）
# ══════════════════════════════════════════════════════════════════


def default_kv_rules() -> OcrKvPatternRules:
    """由 ``core.constants`` 构造 KV 规则（**YAML 缺失时的兜底**）。

    Returns:
        :class:`core.rule_repository.OcrKvPatternRules`；与 repo 从
        ``rules/ocr_kv_patterns.yaml`` 加载的结果**等价**（回归锁断言）。
    """
    return OcrKvPatternRules(
        field_aliases=tuple(
            (name, tuple(aliases)) for name, aliases in C.DEFAULT_KV_FIELD_ALIASES
        ),
        separators=tuple(C.DEFAULT_KV_SEPARATORS),
        whitespace_separator_min=int(C.DEFAULT_KV_WHITESPACE_SEPARATOR_MIN),
        noise_terms=tuple(C.DEFAULT_KV_NOISE_TERMS),
        allow_next_line_value=bool(C.DEFAULT_KV_ALLOW_NEXT_LINE_VALUE),
        source_path="",
    )


def default_blacklist() -> list[str]:
    """字段名黑名单兜底（复用 ``constants.DEFAULT_FIELD_BLACKLIST``）。"""
    return list(C.DEFAULT_FIELD_BLACKLIST)


def default_non_brand_tokens() -> list[str]:
    """非品牌裸 token 兜底（复用 ``constants.DEFAULT_NON_BRAND_TOKENS``）。"""
    return list(C.DEFAULT_NON_BRAND_TOKENS)


# ══════════════════════════════════════════════════════════════════
#  提取器
# ══════════════════════════════════════════════════════════════════


class KvExtractor:
    """把 OCR 行解析为键值对（**归档 + 展示用，不参与判定**）。

    Args:
        rules: 规则仓库；给定时从中取 ``ocr_kv_patterns`` 与
            ``fields_blacklist`` / ``non_brand_tokens``（**用户外置可覆盖**）。
            ``None`` 时使用 :func:`default_kv_rules` + constants 兜底。
        kv_rules: 直接指定 KV 规则快照（优先于 ``rules``，供测试注入）。
        blacklist: 直接指定字段名黑名单（优先于 ``rules``）。
        non_brand: 直接指定非品牌裸 token（优先于 ``rules``）。
    """

    def __init__(
        self,
        rules: RuleRepository | None = None,
        *,
        kv_rules: OcrKvPatternRules | None = None,
        blacklist: list[str] | None = None,
        non_brand: list[str] | None = None,
    ) -> None:
        snapshot = rules.get() if rules is not None else None

        self.rules: OcrKvPatternRules = (
            kv_rules
            if kv_rules is not None
            else (snapshot.ocr_kv_patterns if snapshot is not None else default_kv_rules())
        )

        if blacklist is not None:
            self._blacklist = {_norm(b) for b in blacklist if _norm(b)}
        elif snapshot is not None:
            self._blacklist = {_norm(b) for b in snapshot.fields_blacklist.terms if _norm(b)}
        else:
            self._blacklist = {_norm(b) for b in default_blacklist() if _norm(b)}

        if non_brand is not None:
            self._non_brand = {_norm(t) for t in non_brand if _norm(t)}
        elif snapshot is not None:
            self._non_brand = {_norm(t) for t in snapshot.non_brand_tokens.tokens if _norm(t)}
        else:
            self._non_brand = {_norm(t) for t in default_non_brand_tokens() if _norm(t)}

        # 别名索引：归一化别名 → 规范字段名（用于识别「这是不是一个字段键」）
        self._alias_index: dict[str, str] = {}
        for name, aliases in self.rules.field_aliases:
            self._alias_index.setdefault(_norm(name), name)
            for alias in aliases:
                self._alias_index.setdefault(_norm(alias), name)

        self._noise = {t.strip().lower() for t in self.rules.noise_terms if t.strip()}
        self._separators = tuple(
            sorted((s for s in self.rules.separators if s), key=len, reverse=True)
        )
        self._ws_re = re.compile(
            f"{_WHITESPACE_CLASS}{{{max(1, int(self.rules.whitespace_separator_min))},}}"
        )
        self._log = get_logger(Phase.OCR)

    # ───────────────────── 公共入口 ─────────────────────

    def extract(self, lines: list[Any]) -> tuple[dict[str, str], list[str]]:
        """从 OCR 行提取 ``(kv, residue)``。

        Args:
            lines: OCR 行列表；元素可为 :class:`OcrLine` / ``str`` /
                带 ``text`` 属性的对象 / ``(text, score, box)`` 元组。

        Returns:
            ``(kv, residue)``：键值对字典（保留原始键文本）与未能识别为 KV 的行。
        """
        coerced = [self._coerce(item) for item in (lines or [])]
        kv: dict[str, str] = {}
        residue: list[str] = []

        i = 0
        total = len(coerced)
        while i < total:
            text = (coerced[i].text or "").strip()
            if not text:
                i += 1
                continue
            if text.lower() in self._noise:
                residue.append(text)
                i += 1
                continue

            parsed = self._parse_line(text)
            if parsed is not None:
                key, value = parsed
                _put_first(kv, key, value)
                i += 1
                continue

            # 「值在下一行」（默认关闭；见 DEFAULT_KV_ALLOW_NEXT_LINE_VALUE）
            if self.rules.allow_next_line_value:
                key = self._bare_key(text)
                if key is not None and i + 1 < total:
                    nxt = (coerced[i + 1].text or "").strip()
                    if (
                        nxt
                        and nxt.lower() not in self._noise
                        and self._parse_line(nxt) is None
                    ):
                        _put_first(kv, key, self._clean_value(nxt))
                        i += 2
                        continue

            residue.append(text)
            i += 1

        return kv, residue

    def extract_from_ocr(self, ocr: Any) -> tuple[dict[str, str], list[str]]:
        """便捷入口：直接从 :class:`core.models.OcrText` 提取 KV。

        逐行 ``score`` 取 ``line_scores``（缺失时退化为整体 ``confidence``），
        逐行 ``box`` 取 ``boxes``。

        Args:
            ocr: :class:`core.models.OcrText`。

        Returns:
            ``(kv, residue)``。
        """
        texts = ocr.lines() if hasattr(ocr, "lines") else []
        boxes = list(getattr(ocr, "boxes", None) or [])
        scores = list(getattr(ocr, "line_scores", None) or [])
        confidence = float(getattr(ocr, "confidence", 0.0) or 0.0)
        lines: list[OcrLine] = []
        for idx, text in enumerate(texts):
            score = float(scores[idx]) if idx < len(scores) else confidence
            box = boxes[idx] if idx < len(boxes) else None
            lines.append(OcrLine(text=text, score=score, box=box))
        return self.extract(lines)

    # ───────────────────── 内部实现 ─────────────────────

    @staticmethod
    def _coerce(item: Any) -> OcrLine:
        """把任意输入行规范化为 :class:`OcrLine`。"""
        if isinstance(item, OcrLine):
            return item
        if isinstance(item, str):
            return OcrLine(text=item)
        if isinstance(item, (tuple, list)):
            text = str(item[0]) if item else ""
            score = float(item[1]) if len(item) > 1 and item[1] is not None else 0.0
            box = item[2] if len(item) > 2 else None
            return OcrLine(text=text, score=score, box=box)
        text = getattr(item, "text", None)
        if text is not None:
            score = getattr(item, "score", 0.0)
            try:
                score = float(score or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            return OcrLine(text=str(text), score=score, box=getattr(item, "box", None))
        return OcrLine(text=str(item))

    def _parse_line(self, text: str) -> tuple[str, str] | None:
        """尝试把一行解析为 ``(原始键, 净化的值)``；无法解析返回 ``None``。"""
        found = self._find_separator(text)
        if found is not None:
            idx, sep = found
            key = text[:idx].strip()
            value = text[idx + len(sep) :].strip()
            if self._is_field_key(key):
                return key, self._clean_value(value)
            return None

        # 弱分隔：连续 ≥N 空白，且左侧须命中别名白名单
        match = self._ws_re.search(text)
        if match is not None:
            key = text[: match.start()].strip()
            value = text[match.end() :].strip()
            if _norm(key) in self._alias_index:
                return key, self._clean_value(value)
        return None

    def _find_separator(self, text: str) -> tuple[int, str] | None:
        """返回最先出现的分隔符 ``(位置, 分隔符)``；无则 ``None``。

        多分隔符同时出现时取位置最靠前者；位置相同取较长者。
        """
        best_idx = -1
        best_sep = ""
        for sep in self._separators:
            idx = text.find(sep)
            if idx < 0:
                continue
            if best_idx < 0 or idx < best_idx or (idx == best_idx and len(sep) > len(best_sep)):
                best_idx = idx
                best_sep = sep
        if best_idx < 0:
            return None
        return best_idx, best_sep

    def _is_field_key(self, key: str) -> bool:
        """判定左侧文本是否可作「字段键」。"""
        k = (key or "").strip()
        if not k:
            return False
        if _norm(k) in self._alias_index:
            return True
        if len(k) > _KV_FIELD_KEY_MAX_LEN:
            return False
        if not KV_FIELD_KEY_RE.match(k):
            return False
        return any(ch.isalnum() for ch in k)

    def _bare_key(self, text: str) -> str | None:
        """整行恰为一个已知字段键（用于「值在下一行」）时返回该键，否则 ``None``。"""
        k = (text or "").strip()
        if not k or self._find_separator(k) is not None:
            return None
        return k if _norm(k) in self._alias_index else None

    def _clean_value(self, value: str) -> str:
        """清洗值：去空白；命中字段名黑名单 / 非品牌裸 token 则置空。"""
        v = (value or "").strip()
        if not v:
            return ""
        norm = _norm(v)
        if norm in self._blacklist or norm in self._non_brand:
            return ""
        return v


def _put_first(kv: dict[str, str], key: str, value: str) -> None:
    """写入 KV：首次出现优先；已存在但为空且新值非空时补上（不覆盖已有非空值）。"""
    if key not in kv:
        kv[key] = value
        return
    if not kv[key] and value:
        kv[key] = value


def extract_kv(
    lines: list[Any],
    rules: RuleRepository | None = None,
) -> tuple[dict[str, str], list[str]]:
    """模块级便捷函数：从 OCR 行提取 ``(kv, residue)``。

    Args:
        lines: OCR 行列表。
        rules: 规则仓库（缺省用内置兜底）。

    Returns:
        ``(kv, residue)``。
    """
    return KvExtractor(rules).extract(lines)
