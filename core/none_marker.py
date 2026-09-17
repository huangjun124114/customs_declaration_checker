"""图片侧「显式无标记」识别（core.none_marker，口径 v0.3.2）。

**唯一职责**：在**图片 OCR 全文**里找出**显式的**「无品牌 / 无型号」标记，并回吐
「哪个要素、命中什么原文、在哪张图、原文行是什么」——供 :mod:`core.judge_engine`
在「申报侧为无/缺该要素」时用作**一致证据**。

## 口径（用户裁定 2026-09-17）

    申报要素中「品牌」为无、或压根没有该要素（解析结果为空串）
      ＋ 图片 OCR 中有「无品牌」之类**显式标记**
      → **品牌核验通过**（型号同理）

## 与 :func:`core.noise_guard.is_none_token` 的分工（**不可混淆**）

    ==========================  ====================================================
    函数                        作用对象
    ==========================  ====================================================
    ``is_none_token``           **单个值**是否等价于"无"（判定"值"的语义）
    本模块 ``detect_*``          **整票图的 OCR 全文**里是否存在显式"无"**标记**
    ==========================  ====================================================

本模块只**识别证据**，**不做任何判定** —— 判定在 ``JudgeEngine``。

## ⚠️ 优先级（用户裁定）

显式「无」标记是**标签本体的直接证据**，其效力**高于**同一票图中其他位置识别到的
品牌/型号文字（唛头 ``SKYWORTH P/N``、外箱 ``Brand:Daewoo`` 等）—— 后者仍写进
「判定依据」列留痕，仅供人工追溯。**这与「不虚高」红线方向一致**：不虚高 = 不把
"没找到证据"当"有证据不一致"。

## ⚠️ 防虚高护栏

正则的**值侧必须落在行尾或分隔符边界**上（见 ``rules/brand_patterns.yaml`` 注释）：
缺此护栏时 ``品牌:无锡机电`` 会因前缀 ``无`` 被误判为「无」标记。

## 规则外置

正则**全部**来自注入的 :class:`core.rule_repository.RuleRepository`
（``brand_patterns.yaml::none_markers``）；未注入 / YAML 缺该节点时回退
:data:`core.constants.DEFAULT_NONE_MARKERS`（**兜底 ≡ repo**，缺陷 F 的教训）。

依赖：``core.models`` / ``core.constants`` / ``core.rule_repository``（**不依赖 PySide6**）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core import constants
from core.models import OcrText
from core.rule_repository import RuleRepository

__all__ = [
    "NoneMarkerHit",
    "iter_none_marker_rules",
    "detect_none_marker",
    "detect_none_markers",
]


#: 原文行截断长度（供 UI 展示 / JSON 追溯，与 ``token_matcher`` 同口径）
_SAMPLE_LINE_LIMIT: int = 120

#: 图片文件名中的序号组（如 ``A&001.jpg`` 的 ``001``）。
#: ⚠️ 与 :mod:`core.token_matcher` 的同名兜底逻辑一致 —— 管线产出的 ``OcrText.seq``
#: 恒为 ``0``（序号只落在 ``ImageEvidence.seq``），故此处按文件名兜底解析。
_IMAGE_SEQ_RE = re.compile(r"[&](?:0*)(\d+)(?=\.[^.]*$|$)")

#: 编译缓存：``{规则元组: [(name, field, pattern, note), ...]}``。
#: 规则快照不可变且启动一次性加载，按内容缓存可安全复用（**不做热加载**）。
_COMPILED_CACHE: dict[tuple[tuple[str, str, str, str], ...], tuple[Any, ...]] = {}


def _truncate(text: str, limit: int = _SAMPLE_LINE_LIMIT) -> str:
    """截断超长文本（保留尾部省略号）。"""
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


def _resolve_image_seq(text: Any) -> int:
    """取某张图的序号（优先 ``OcrText.seq``，为 0 时回退解析文件名 ``&NNN``）。

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
    match = _IMAGE_SEQ_RE.search(str(getattr(text, "image_path", "") or ""))
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass(frozen=True)
class NoneMarkerHit:
    """一次「显式无标记」命中（**本身不占列**：只进「判定说明」/「判定依据」/ JSON）。

    Attributes:
        field: 所属要素（``品牌`` / ``型号``）。
        marker_name: 命中的规则名（``brand_labeled`` / ``model_bare`` …，供追溯）。
        marker: 命中的标记原文（如 ``品牌:无`` / ``无型号``）。
        image: 命中所在图片序号（0 表示无法确定）。
        line: 命中所在 OCR 原文行（截断 120 字）。
        note: 规则自带的说明。
    """

    field: str = ""
    marker_name: str = ""
    marker: str = ""
    image: int = 0
    line: str = ""
    note: str = ""

    @property
    def description(self) -> str:
        """人类可读的一行证据描述（进「判定说明」）。"""
        where = f"图 {self.image}" if self.image > 0 else "图号未知"
        return f"图片中显式标注『{self.marker}』（{where}）"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供详细 JSON 日志落盘）。"""
        return {
            "field": self.field,
            "marker_name": self.marker_name,
            "marker": self.marker,
            "image": self.image,
            "line": self.line,
            "note": self.note,
        }


def iter_none_marker_rules(
    rules: RuleRepository | None = None,
) -> tuple[tuple[str, str, re.Pattern[str], str], ...]:
    """取出并编译当前生效的「显式无标记」规则。

    **权威载体**为 ``rules/brand_patterns.yaml::none_markers``；``rules`` 为 ``None``
    或该节点缺失时，回退 :data:`core.constants.DEFAULT_NONE_MARKERS`（兜底 ≡ repo）。

    Args:
        rules: 规则仓库；``None`` 时使用兜底常量。

    Returns:
        ``((name, field, compiled_pattern, note), ...)``；无任何规则时为空元组。
    """
    raw: tuple[tuple[str, str, str, str], ...] = ()
    if rules is not None:
        try:
            raw = tuple(rules.get().brand_patterns.none_markers)
        except Exception:  # noqa: BLE001 - 规则加载失败一律退兜底，保证不崩
            raw = ()
    if not raw:
        raw = tuple(constants.DEFAULT_NONE_MARKERS)

    cached = _COMPILED_CACHE.get(raw)
    if cached is not None:
        return cached

    compiled = tuple(
        (name, field, re.compile(regex, re.IGNORECASE), note)
        for name, field, regex, note in raw
        if regex
    )
    _COMPILED_CACHE[raw] = compiled
    return compiled


def detect_none_markers(
    texts: list[OcrText] | None,
    *,
    rules: RuleRepository | None = None,
) -> dict[str, NoneMarkerHit]:
    """扫描**整票图的 OCR 全文**，找出每个要素的首个「显式无标记」。

    扫描顺序**确定**（图序 → 行序 → 规则序），同一要素只保留**首次**命中，
    保证同输入结果可复现（可追溯红线）。

    ⚠️ **逐图逐行**匹配，绝不跨图拼接后检索（防跨图污染，与 ``whole_machine`` 同原则）。

    Args:
        texts: 该记录的 OCR 结果列表。
        rules: 规则仓库；``None`` 时用兜底常量。

    Returns:
        ``{字段名: NoneMarkerHit}``；无命中时为空字典。
    """
    marker_rules = iter_none_marker_rules(rules)
    hits: dict[str, NoneMarkerHit] = {}
    if not marker_rules:
        return hits

    for text in texts or []:
        if text is None:
            continue
        raw = getattr(text, "text_raw", "") or ""
        if not raw:
            continue
        seq = _resolve_image_seq(text)
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for name, field, pattern, note in marker_rules:
                if field in hits:
                    continue
                match = pattern.search(stripped)
                if match is None:
                    continue
                hits[field] = NoneMarkerHit(
                    field=field,
                    marker_name=name,
                    marker=match.group(0).strip(),
                    image=seq,
                    line=_truncate(stripped),
                    note=note,
                )
    return hits


def detect_none_marker(
    texts: list[OcrText] | None,
    field: str,
    *,
    rules: RuleRepository | None = None,
) -> NoneMarkerHit | None:
    """单要素便捷入口：取某字段的「显式无标记」命中（无则 ``None``）。

    Args:
        texts: 该记录的 OCR 结果列表。
        field: 要素名（``品牌`` / ``型号``）。
        rules: 规则仓库；``None`` 时用兜底常量。

    Returns:
        :class:`NoneMarkerHit` 或 ``None``。
    """
    if not field:
        return None
    return detect_none_markers(texts, rules=rules).get(field)
