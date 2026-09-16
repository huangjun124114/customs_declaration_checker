"""判定引擎（core.judge_engine，对应架构设计第 4 节 + SOP 1.3 / 3.4）。

**唯一职责**：把「申报侧（``decl_brand`` / ``decl_model``）× 图片侧（OCR 值）」按
SOP 1.3 四类判定口径与 SOP 3.4 比对 5 条，产出 :class:`core.models.CheckResult`。

## 契约（写死，见架构设计 13.13.2）
  * ``judge(record, ocr_texts) -> CheckResult``：**纯函数，无副作用**，可重复调用结果一致。
  * 申报侧取值**仅**用 ``record.decl_brand`` / ``record.decl_model``；
    **禁止** import :mod:`core.element_parser` 或调用 ``parse``（保持"解析在 T02、
    判定在 T04"的单向依赖）。
  * ``record.raw_element_text`` 的合法用途**仅限**：填充 ``DifferenceDetail`` 的
    证据回溯串与 ``CheckResult.evidence_text`` 片段；**不得**作为比对算法输入。
  * 图片侧取值：**v0.3.0 起以「申报值是否在 OCR 全文里以完整分词出现」为主取证**
    （:class:`core.token_matcher.TokenMatcher`）；``extract_detected_brand`` /
    ``extract_detected_model`` **降级**为「图内是否存在同类标识」的判据与展示值。
  * 完整 OCR 原文**只进 JSON**（不进日志）。

## SOP 3.4 比对 5 条 → 实现映射（v0.3.0）

    ============================================  ==============================
    SOP 3.4 规则                                  实现分支
    ============================================  ==============================
    ① 双方均为无/空 → 合格                        ``_both_absent``
    ② 申报值在图中以完整分词出现 → 合格            ``TokenMatcher`` 命中（EXACT/FUZZY）
    ③ 图内有同类标识但值不同 → 异常                两级分流 → ``MISMATCH``
    ④ 申报缺失但图片明确有 → 异常                 ``detected 有值 且 declared 无``
    ⑤ 型号不一致须列逐字符差异                    :mod:`core.diff_util`
    ============================================  ==============================

**不虚高红线**：任何 ``NoiseLevel.SUSPICIOUS`` 的记录**强制** ``Verdict.NO_MARK``（⚠️），
**严禁**直达 ``Verdict.FAIL``（❌）—— 由 :meth:`JudgeEngine.judge` 末尾的
"降级护栏"统一保证（架构设计 12.R5②）。

## ⚠️ 口径变更声明（v0.3.0，2026-09-16 用户拍板）

本模块**首次**改动判定主链路：由「**先提取、后比对**」改为「**完整分词直接命中**」。
变更范围**严格限定**在"字段状态如何产生"（:meth:`JudgeEngine._field_state_v03`）：

  * **不变**：图片证据闸门（🔵）、整机品牌保留 ❌（T06 裁决 A′）、记录级跨文字体系兜底、
    ``SUSPICIOUS`` 降级护栏、四类口径字符串、13 列汇总表结构、:mod:`core.noise_guard`
    的比对语义（``classify_detail`` 仍作为**未命中时的兜底三态**继续生效）；
  * **变更**：取证方式 —— 命中判定不再依赖"猜出图片侧字段值"，消除 OCR 无空间感知
    带来的 key-value 缺失误判。新基线须实测确立（见 ``docs/10_迭代方案_v0.3_0916.md``）。
"""

from __future__ import annotations

from typing import Any

from core import constants
from core.diff_util import char_diff
from core.models import (
    CheckResult,
    DeclarationRecord,
    DifferenceDetail,
    ImageEvidence,
    NoiseLevel,
    OcrText,
    Verdict,
)
from core.noise_guard import NoiseGuard, is_none_token
from core.rule_repository import RuleRepository
from core.token_matcher import TokenMatch, TokenMatcher
from infra.logger import Phase, get_logger

__all__ = [
    "JudgeEngine",
    "extract_detected_brand",
    "extract_detected_model",
    "_load_non_brand_tokens",
    "_load_field_name_terms",
    "_is_field_name_fragment",
    "_vote_brand",
]


#: 品牌字段名（**别名** —— 唯一来源是 :data:`core.constants.FIELD_BRAND`，
#: 供差异明细 / ``TokenMatch.field`` / UI 三列表共用，由 ``test_constants.py`` 锁一致）
_FIELD_BRAND = constants.FIELD_BRAND
#: 型号字段名（同上）
_FIELD_MODEL = constants.FIELD_MODEL

# ── 字段级状态常量（SOP 3.4 规则①②③④ 的字段级落点）──
#: 双方均为"无" → 合格
_FIELD_BOTH_ABSENT = "BOTH_ABSENT"
#: 申报值在图中以**完整分词**命中（v0.3.0 主路径）→ 合格
_FIELD_MATCH = "MATCH"
#: 疑似噪声 → ⚠️（绝不 ❌，不虚高红线）
_FIELD_SUSPICIOUS = "SUSPICIOUS"
#: 图片侧无**同类标识**（申报有值，但图内无该文字）→ ⚠️ 缺图内标识
_FIELD_DETECTED_MISSING = "DETECTED_MISSING"
#: 申报缺失但图片明确有 → ❌
_FIELD_DECLARED_MISSING = "DECLARED_MISSING"
#: 图内有**同类标识**但值不同（两级分流的 ❌ 分支）→ ❌
_FIELD_MISMATCH = "MISMATCH"

#: 品牌值不得是这些字段名/标签（避免跨标签误捕，如 ``品牌: 型号:``）
_LABEL_WORDS: frozenset[str] = frozenset(
    {"型号", "规格型号", "产品型号", "制造商型号", "数量", "品牌", "BRAND", "MODEL", "QTY"}
)

#: 图片侧品牌提取：``品牌:xxx`` / ``Brand:xxx`` / ``品牌：xxx`` / ``xxx牌``
_BRAND_PATTERNS: tuple[str, ...] = (
    r"(?:品牌|Brand|BRAND)\s*[:：]\s*([A-Za-z0-9\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5\-\.\/]*)",
    r"([A-Za-z0-9\u4e00-\u9fa5]{2,20})牌",
)

#: 品牌值最小有效长度（单字符捕获多为"无值 token"被截断的残片，如 ``N/A`` → ``N``）
_MIN_BRAND_LEN: int = 2

#: 图片侧「P/N（料号）」行提取：``SKYWORTH P/N`` / ``SKYHORTH P/H`` / ``IFR PIN``
#:
#: ⚠️ 实测（架构设计 13.5）标签上常以 ``{品牌} P/N`` 一整行出现，OCR 会连品牌一起误读
#: （``SKYWORTH P/N`` → ``SKYHORTH P/H``）。该行整体作为"品牌相关证据"参与噪声比对，
#: 由 :mod:`core.noise_guard` 的易混字符 / 模糊相似度规则判为 ``SUSPICIOUS``（⚠️）。
_PN_LINE_PATTERNS: tuple[str, ...] = (
    r"^([A-Za-z0-9][A-Za-z0-9\-\s]{2,40}?)\s*P\s*/\s*[NH]\b",
    r"^([A-Za-z0-9][A-Za-z0-9\-]{2,40}?)\s+P\s?I\s?N\b",
)

#: ``{品牌} {型号后缀}`` 行（无标签）：``boori E339609`` / ``baori E339609``。
#:
#: ⚠️ 实测纠正（缺陷 C）：原正则 ``^([A-Za-z][A-Za-z\-]{2,29})\s+[A-Za-z0-9]...$``
#: 会**误吞字段名对**（``Production Date`` / ``Supplier Code`` / ``Serial No.`` /
#: ``Congo Something`` 等），导致 ``Manufacturer`` / ``Serial`` 等被当品牌值。
#: 收紧策略：
#:   * 第二个 token **必须含数字**（型号特征，如 ``E339609``）——纯字母对（字段名）被排除；
#:   * 第一个 token **不得是已知字段词**（``_LABEL_WORDS`` / 字段名黑名单语境）。
_BRAND_MODEL_LINE_RE = r"^([A-Za-z][A-Za-z\-]{2,29})\s+(?=[A-Za-z0-9\-_\.]*\d)[A-Za-z0-9][A-Za-z0-9\-_\.]{2,39}$"

#: 明显是"料号 / 订单号"的形态（含连续数字段），**不得**被当作品牌裸 token
_PART_NUMBER_LIKE_RE = r"\d{3,}"

#: 图片侧型号提取：``型号:xxx`` / ``Model:xxx`` / ``MODEL:xxx``
#:
#: ⚠️ 缺陷 A / 口径问题 1 裁决（架构裁决文档 1.3 / 6.1）：**已移出**
#: ``Customermodel`` / ``Customer model`` —— 该字段是"客户/整机型号"，**不构成本体
#: 型号证据**（SOP 3.5#8 同理会旨、SOP 陷阱#8）。本体型号证据来源优先级：
#: ``型号/规格型号/产品型号/制造商型号``（标签）> PCB ``MODEL:`` 丝印 > 其他。
_MODEL_PATTERNS: tuple[str, ...] = (
    r"(?:型号|规格型号|产品型号|制造商型号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-_\.]{0,39})",
    r"(?:Model|MODEL)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-_\.]{0,39})",
)

#: 独立的"裸" token 行（无任何标签）：如 ``YUTONG`` / ``Daewoo``。
#: **必须纯 ASCII 字母（可含连字符/点），不含数字** —— 料号/订单号不会命中。
_BARE_TOKEN_RE = r"^(?:[A-Za-z][A-Za-z\-\.]{2,29}|[\u4e00-\u9fa5]{2,20})$"

#: 已知的非品牌裸 token（OCR 常见字段名/单位，避免误捕）。
#:
#: ⚠️ 缺陷 C 裁决（架构裁决文档 4.3 / 11.③）：此为 **YAML 缺失时的兜底常量**，
#: **不作权威载体**。权威载体是 ``rules/non_brand_tokens.yaml``；运行时优先从
#: :class:`core.rule_repository.RuleRepository` 读取（见 :func:`_load_non_brand_tokens`）。
_NON_BRAND_BARE_TOKENS: frozenset[str] = frozenset(
    {
        "CARTON",
        "JOBNO",
        "QTY",
        "MADE",
        "CHINA",
        "ORIGIN",
        "CODE",
        "DATE",
        "MODEL",
        "BRAND",
        "PCS",
        "CTN",
    }
)

#: 分层优先级（缺陷 C 裁决，架构裁决文档 4.2）：
#: ``① 显式标签 > ② P/N 行 > ③ 品牌+型号行 > ④ 裸 token``。
#: 高优先级层一旦有候选，低优先级层**不参与投票**。
_TIER_LABEL: int = 0
_TIER_PN: int = 1
_TIER_BRAND_MODEL: int = 2
_TIER_BARE: int = 3

#: 跨图一致性投票：候选须在 **≥ 该数量**张不同图中出现才采信（缺陷 C）。
#: 单图孤例（如 ``ONEL``）→ 不采信，防单图噪声。
_CONSISTENCY_MIN_IMAGES: int = 2

#: 标签图加权（``&001``，通常为标签图/外箱唛头，SOP 3.3「信息最全」）权重 ×2。
_LABEL_IMAGE_WEIGHT: int = 2

#: 图片文件名中的序号组（如 ``A&001.jpg`` 的 ``001``）。
_IMAGE_SEQ_RE = r"[&](?:0*)(\d+)\s*$"


def _is_label_image(image_path: str) -> bool:
    """判断某图片是否为标签图 ``&001``（SOP 3.3：信息最全 → 投票加权）。

    Args:
        image_path: 图片路径（``...&001.jpg`` 形式）。

    Returns:
        ``True`` 表示该图序号为 ``1``（``&001``）。
    """
    import re

    name = str(image_path or "").rsplit(".", 1)[0]
    match = re.search(_IMAGE_SEQ_RE, name)
    if not match:
        return False
    try:
        return int(match.group(1)) == 1
    except (TypeError, ValueError):
        return False


def _normalize_candidate(value: str) -> str:
    """候选值归一化（用于投票计数，upper + 去分隔符）。

    Args:
        value: 原始候选品牌值。

    Returns:
        归一化后的键（空串表示无效候选）。
    """
    raw = ("" if value is None else str(value)).strip().upper()
    for ch in (" ", "\t", "/", "-", "_", ".", ":"):
        raw = raw.replace(ch, "")
    return raw


def _vote_brand(
    candidates: list[tuple[str, str, str]],
    *,
    min_images: int = _CONSISTENCY_MIN_IMAGES,
) -> str:
    """**跨图一致性投票**（缺陷 C 裁决，架构裁决文档 4.2）。

    投票规则：

      1. 归一化（upper + 去分隔符）后按 **加权票数** 决胜；
      2. 候选须在 **≥ ``min_images`` 张不同图**中出现才采信；
      3. 标签图 ``&001`` 的候选 **权重 ×2**（``_LABEL_IMAGE_WEIGHT``）；
      4. 无任何候选满足 → 返回空串（由上层判 ⚠️，**绝不**用单图噪声值）。

    ⚠️ ``min_images`` 分层取值（关键实现细节）：

      * **裸 token 层（④）**：``min_images=2`` —— 裸 token 是噪声主源
        （``ONEL``/``WLO``/``NFK``/``PASSED``/``AWM`` 等单图孤例），必须 ≥2 图一致；
        与架构裁决文档 4.5 ``test_single_image_noise_rejected`` 一致。
      * **显式标签（①）/ P\\N（②）/ 品牌+型号（③）层**：``min_images=1`` ——
        这些层本身已是强证据（显式 ``Brand:xxx`` 标签），SOP 3.3「必须全量 OCR」
        并不要求"每张图都出现"；若强制 ≥2 图会**漏掉只出现在单张标签图上的真品牌**
        （实测 seq=2/9/12/13/14 的真品牌仅出现在 1 张唛头图）。

    Args:
        candidates: ``[(归一化键, 原形值, image_id), ...]``（同图同层候选已去重）。
        min_images: 采信所需的最小不同图片数。

    Returns:
        胜出的品牌值（**保留原形**）；无满足条件者返回 ``""``。
    """
    if not candidates:
        return ""

    # 归一化键 -> {"display": 代表原形, "weight": 加权票, "images": 图集合}
    stats: dict[str, dict[str, Any]] = {}
    for norm_key, display, image_id in candidates:
        if not norm_key:
            continue
        bucket = stats.setdefault(
            norm_key, {"display": display, "weight": 0, "images": set()}
        )
        bucket["weight"] += _LABEL_IMAGE_WEIGHT if _is_label_image(image_id) else 1
        bucket["images"].add(image_id)
        # 优先保留"更像品牌"的原形（含大小写混合者优先，否则保留首个）
        if not bucket["display"] or (
            bucket["display"].isupper() and not display.isupper()
        ):
            bucket["display"] = display

    # 过滤：须在 ≥ min_images 张不同图中出现（跨图一致性）
    qualified = {
        key: bucket
        for key, bucket in stats.items()
        if len(bucket["images"]) >= max(1, min_images)
    }
    if not qualified:
        return ""

    # 决胜：加权票数最多 → 键字典序最小（确定性，保证可复现）
    top_weight = max(b["weight"] for b in qualified.values())
    top_keys = [k for k, b in qualified.items() if b["weight"] == top_weight]
    best_key = sorted(top_keys)[0]
    return qualified[best_key]["display"]


def _load_non_brand_tokens(rules: RuleRepository | None = None) -> frozenset[str]:
    """加载"非品牌裸 token"词表（缺陷 C 裁决，架构裁决文档 4.3）。

    **权威载体**为 ``rules/non_brand_tokens.yaml``；``rules`` 为 ``None`` 或 YAML
    缺失时，回退到 :data:`_NON_BRAND_BARE_TOKENS` 兜底常量（**非权威载体**）。

    Args:
        rules: 规则仓库；``None`` 时使用兜底常量。

    Returns:
        大写化后的 token 集合。
    """
    tokens: tuple[str, ...] = ()
    if rules is not None:
        try:
            loaded = rules.get().non_brand_tokens.tokens
            if loaded:
                tokens = tuple(loaded)
        except Exception:  # noqa: BLE001 - 规则加载失败一律退兜底，保证不崩
            tokens = ()
    if not tokens:
        tokens = tuple(_NON_BRAND_BARE_TOKENS)
    return frozenset(str(t).strip().upper() for t in tokens if str(t).strip())


def _collect_brand_candidates(
    texts: list[OcrText],
) -> dict[int, list[tuple[str, str, str]]]:
    """**收集全部品牌的候选**（缺陷 C 裁决：不再"首个命中即返回"）。

    分层收集（架构裁决文档 4.2）：① 显式标签 / ② ``P/N`` 行 / ③ 品牌+型号行 /
    ④ 裸 token 行。每层返回 ``[(归一化键, 原形值, image_id), ...]``。

    ⚠️ 投票须限定在**同一层内**；且每条候选携带其**来源图片 id**，供"≥2 图一致"
    与"``&001`` 加权"判定（**逐图收集，绝不跨图拼接后检索**）。

    Args:
        texts: 该记录的 OCR 结果列表（等长同序）。

    Returns:
        ``{tier: [(norm_key, display, image_id), ...]}``，tier 取
        :data:`_TIER_LABEL` / :data:`_TIER_PN` / :data:`_TIER_BRAND_MODEL` /
        :data:`_TIER_BARE`。
    """
    import re

    tiers: dict[int, list[tuple[str, str, str]]] = {
        _TIER_LABEL: [],
        _TIER_PN: [],
        _TIER_BRAND_MODEL: [],
        _TIER_BARE: [],
    }

    for text in texts:
        raw = getattr(text, "text_raw", "") or ""
        if not raw:
            continue
        image_id = str(getattr(text, "image_path", "") or "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]

        for line in lines:
            # ① 显式标签：品牌:xxx / Brand:xxx / xxx牌
            for pattern in _BRAND_PATTERNS:
                match = re.search(pattern, line)
                if not match:
                    continue
                value = (match.group(1) or "").strip()
                if _is_valid_brand_value(value):
                    tiers[_TIER_LABEL].append(
                        (_normalize_candidate(value), value, image_id)
                    )
                break

            # ② P/N 行：``SKYWORTH P/N`` / ``SKYHORTH P/H`` / ``IFR PIN``
            #    取品牌前缀供噪声比对识别误读（整行形态）
            for pattern in _PN_LINE_PATTERNS:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if not match:
                    continue
                value = (match.group(1) or "").strip()
                if _is_valid_brand_value(value):
                    tiers[_TIER_PN].append(
                        (_normalize_candidate(value), value, image_id)
                    )
                break

            # ③ 品牌 + 型号后缀行（无标签）：``boori E339609`` → ``boori``
            match = re.match(_BRAND_MODEL_LINE_RE, line)
            if match:
                value = (match.group(1) or "").strip()
                if _is_valid_brand_value(value):
                    tiers[_TIER_BRAND_MODEL].append(
                        (_normalize_candidate(value), value, image_id)
                    )
                continue

            # ④ 独立裸 token 行（如 ``YUTONG`` / ``Daewoo``）
            if not re.match(_BARE_TOKEN_RE, line):
                continue
            if re.search(_PART_NUMBER_LIKE_RE, line):
                continue
            tiers[_TIER_BARE].append(
                (_normalize_candidate(line), line, image_id)
            )

    return tiers


def _is_valid_brand_value(value: str) -> bool:
    """判断候选值是否为"看起来像品牌"的有效值。

    Args:
        value: 捕获到的候选品牌值。

    Returns:
        ``True`` 表示有效（非空、非标签词、非"无"取值、长度达标）。
    """
    text = (value or "").strip()
    if len(text) < _MIN_BRAND_LEN:
        return False
    if text in _LABEL_WORDS or text.upper() in _LABEL_WORDS:
        return False
    if is_none_token(text):
        return False
    return True


def _load_field_name_terms(rules: RuleRepository | None = None) -> frozenset[str]:
    """加载"字段名词表"（``rules/fields_blacklist.yaml::terms``，缺陷 C/口径 2）。

    缺陷 C 裁决要求：**字段名残片不得作本体品牌/型号证据**（架构裁决文档 6.2）。
    该词表用于剔除 P/N 行、品牌+型号行捕获到的**字段名前缀**（如 ``MFR P/N`` →
    ``MFR``、``MWFR P/N`` → ``MWFR``、``Manufacturer P/N`` → ``Manufacturer``）。

    **权威载体**为 ``rules/fields_blacklist.yaml``；``rules`` 为 ``None`` 或 YAML
    缺失时回退到 :data:`_LABEL_WORDS` 兜底常量（**非权威载体**）。

    Args:
        rules: 规则仓库；``None`` 时使用兜底常量。

    Returns:
        大写化后的字段名词集合。
    """
    terms: tuple[str, ...] = ()
    if rules is not None:
        try:
            loaded = rules.get().fields_blacklist.terms
            if loaded:
                terms = tuple(loaded)
        except Exception:  # noqa: BLE001 - 规则加载失败一律退兜底，保证不崩
            terms = ()
    if not terms:
        terms = tuple(_LABEL_WORDS)
    # 归一化（大写 + 去分隔符），与 :func:`_normalize_candidate` 口径一致，
    # 使 ``"MFR P/N"`` → ``MFRPN``、``MFR`` → ``MFR`` 可被子串匹配命中。
    return frozenset(
        _normalize_candidate(t) for t in terms if str(t).strip()
    )


def _is_field_name_fragment(value: str, field_terms: frozenset[str]) -> bool:
    """判断候选值是否为**字段名残片**（缺陷 C/口径 2 的排除闸门）。

    字段名残片来源：P/N 行 / 品牌+型号行正则把 ``{字段名} P/N`` 的字段名前缀捕获为
    "品牌"（如 ``MFR P/N`` → ``MFR``）。这类值**不构成本体品牌证据**（架构裁决文档
    6.2）→ 必须从候选中剔除，否则会制造新误报（实测 seq=8/16）。

    判定：候选值归一化（大写、去分隔符）后，**等于**任一字段名词，或**是**任一字段名
    词的子串/被其包含（容 OCR 残读，如 ``MWFR`` ⊃ ``MFR``）。

    Args:
        value: 候选品牌值。
        field_terms: 大写化字段名词表（见 :func:`_load_field_name_terms`）。

    Returns:
        ``True`` 表示是字段名残片（应剔除）。
    """
    norm = _normalize_candidate(value)
    if not norm:
        return False
    if norm in field_terms:
        return True
    # 容 OCR 误读：候选是字段名的子串，或字段名是候选的子串（长度 ≥3 防误伤）
    for term in field_terms:
        if len(term) >= 3 and (term in norm or norm in term):
            return True
    return False


def extract_detected_brand(
    texts: list[OcrText],
    rules: RuleRepository | None = None,
) -> str:
    """从 OCR 文本列表中提取**图片侧识别品牌**（缺陷 C：跨图一致性投票）。

    提取流程（缺陷 C 裁决，架构裁决文档 4.2）：

      1. **收集全部候选**（不再"首个命中即返回"）：遍历**所有图**的所有行，
         按 ① 显式标签 / ② ``P/N`` 行 / ③ 品牌+型号行 / ④ 裸 token 四层收集；
      2. **分层优先级**：``① > ② > ③ > ④``；高优先级层一旦有候选，
         低优先级层**不参与投票**（如显式 ``Brand:Daewoo`` 存在时，
         不再让 ``ONEL`` 等裸 token 干扰）；
      3. **跨图一致性投票（层内）**：归一化后加权票数最多者胜；
         **裸 token 层须在 ≥2 张不同图中出现**（单图孤例不采信，防 ``ONEL`` 类单图噪声）；
         **标签 / P\\N / 品牌+型号层**为强证据，单图即可（若强制 ≥2 图会漏掉只出现在
         单张唛头图上的真品牌，如实测 seq=2/9/12/13/14）；
      4. **字段名残片剔除**：①/②/③ 层捕获到的**字段名前缀**（``MFR P/N`` → ``MFR`` 等）
         依 :func:`_is_field_name_fragment` 剔除（§6.2 口径 2）；
      5. **标签图加权**：``&001``（SOP 3.3「信息最全」）的候选权重 ×2；
      6. **落空处置**：无满足者 → 返回 ``""`` → 上层判 ⚠️（**绝不用单图噪声值**）。

    Args:
        texts: 该记录的 OCR 结果列表（等长同序，见 ``OcrEngine.recognize_batch``）。
        rules: 规则仓库（供加载 :data:`_NON_BRAND_BARE_TOKENS` 的权威词表）；
            ``None`` 时用兜底常量。

    Returns:
        识别到的品牌值；未识别到（或无一致证据）返回 ``""``。
    """
    if not texts:
        return ""

    non_brand = _load_non_brand_tokens(rules)
    field_terms = _load_field_name_terms(rules)
    tiers = _collect_brand_candidates(texts)

    # ①/②/③ 层须剔除"字段名残片"（缺陷 C/口径 2：``MFR P/N`` → ``MFR`` 不作证据）。
    # ④ 裸 token 层须剔除"非品牌 token"（权威词表优先）。
    tiers[_TIER_LABEL] = [
        (norm, display, image_id)
        for norm, display, image_id in tiers[_TIER_LABEL]
        if not _is_field_name_fragment(display, field_terms)
    ]
    tiers[_TIER_PN] = [
        (norm, display, image_id)
        for norm, display, image_id in tiers[_TIER_PN]
        if not _is_field_name_fragment(display, field_terms)
    ]
    tiers[_TIER_BRAND_MODEL] = [
        (norm, display, image_id)
        for norm, display, image_id in tiers[_TIER_BRAND_MODEL]
        if not _is_field_name_fragment(display, field_terms)
    ]
    tiers[_TIER_BARE] = [
        (norm, display, image_id)
        for norm, display, image_id in tiers[_TIER_BARE]
        if display.strip().upper() not in non_brand
        and not _is_field_name_fragment(display, field_terms)
    ]

    # 分层优先级：高优先级层有候选则只在该层投票
    # ⚠️ 裸 token 层需 ≥2 图一致（防单图噪声）；其余层单图即可（强证据）。
    tier_min_images: dict[int, int] = {
        _TIER_LABEL: 1,
        _TIER_PN: 1,
        _TIER_BRAND_MODEL: 1,
        _TIER_BARE: _CONSISTENCY_MIN_IMAGES,
    }
    for tier in (_TIER_LABEL, _TIER_PN, _TIER_BRAND_MODEL, _TIER_BARE):
        # 同图同层去重（避免同一张图内重复行刷高票数）
        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str, str]] = []
        for norm, display, image_id in tiers[tier]:
            key = (norm, image_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((norm, display, image_id))
        winner = _vote_brand(deduped, min_images=tier_min_images[tier])
        if winner:
            return winner

    return ""


def extract_detected_model(texts: list[OcrText]) -> str:
    """从 OCR 文本列表中提取**图片侧识别型号**。

    按 :data:`_MODEL_PATTERNS` 顺序尝试，首个命中即返回。
    命中"无"取值时返回空串（表示图片侧无型号标识）。

    Args:
        texts: 该记录的 OCR 结果列表。

    Returns:
        识别到的型号值；未识别到返回 ``""``。
    """
    import re

    for text in texts:
        raw = getattr(text, "text_raw", "") or ""
        if not raw:
            continue
        for pattern in _MODEL_PATTERNS:
            match = re.search(pattern, raw)
            if not match:
                continue
            value = (match.group(1) or "").strip()
            if len(value) < _MIN_BRAND_LEN:
                continue
            if value and not is_none_token(value):
                return value
    return ""


def _normalize(value: str) -> str:
    """规范化比对值（strip + 大写无关化在逐字符差异中保留原形，此处仅 strip）。

    Args:
        value: 原始值。

    Returns:
        strip 后的字符串（``None`` → ``""``）。
    """
    return ("" if value is None else str(value)).strip()


def _match_reasons(
    state: str,
    match: TokenMatch | None,
    field_name: str,
) -> list[str]:
    """把一条字段的完整分词取证说明附进判定理由（v0.3.0 可追溯）。

    只在"说明能解释结论"时附：

      * ``BOTH_ABSENT`` / ``DECLARED_MISSING`` → 不附（由差异明细承载，避免噪声）；
      * ``MATCH`` 走**兼容路径**（未命中却因旧链路一致性判 ✅）→ 不附（说明会自相矛盾）；
      * 其余（命中 ✅ / 未命中 ⚠️ / ❌）→ 附 :attr:`core.token_matcher.TokenMatch.note`。

    Args:
        state: 字段状态常量（:data:`_FIELD_*`）。
        match: 完整分词匹配结果。
        field_name: 字段名（保留参数，供未来细分文案）。

    Returns:
        待追加的说明列表（通常 0–1 条）。
    """
    del field_name  # 说明文案由 TokenMatch 自带（含字段名），此处无需重复
    if match is None:
        return []
    if state in (_FIELD_BOTH_ABSENT, _FIELD_DECLARED_MISSING):
        return []
    note = (getattr(match, "note", "") or "").strip()
    if not note:
        return []
    if state == _FIELD_MATCH and not match.hit:
        return []
    return [note]


class JudgeEngine:
    """四类口径判定引擎（架构设计第 4 节 ``JudgeEngine``）。

    典型用法::

        engine = JudgeEngine(rules=repo)      # 注入规则仓库
        result = engine.judge(record, ocr_texts)
        assert result.verdict in tuple(Verdict)

    Args:
        rules: 规则仓库；``None`` 时 ``NoiseGuard`` 退回 :mod:`core.constants` 兜底默认值。
        noise_guard: 可注入的噪声护栏（**供测试**）；缺省用 ``rules`` 构造。
    """

    def __init__(
        self,
        rules: RuleRepository | None = None,
        *,
        noise_guard: NoiseGuard | None = None,
    ) -> None:
        """构造判定引擎。"""
        self._repo: RuleRepository | None = rules
        self._guard = noise_guard if noise_guard is not None else NoiseGuard(rules)
        self._log = get_logger(Phase.PHASE5)

    @property
    def guard(self) -> NoiseGuard:
        """当前使用的噪声护栏（供上层/测试读取）。"""
        return self._guard

    def _is_whole_machine_any_image(
        self,
        texts: list[OcrText],
        detected_brand: str,
    ) -> bool:
        """**逐图**判定是否命中外箱整机品牌（缺陷 B 约束：不跨图拼接）。

        缺陷 B 裁决要求整机上下文检索范围**限定在同一张图内**。调用方须逐图调用
        :meth:`core.noise_guard.NoiseGuard.is_whole_machine_brand`；任一图命中即为
        整机品牌记录。

        Args:
            texts: 该记录的 OCR 结果列表。
            detected_brand: 图片侧识别品牌（用于精确定位含品牌的短行）。

        Returns:
            ``True`` 表示至少一张图命中"外箱整机品牌"上下文。
        """
        for text in texts:
            raw = getattr(text, "text_raw", "") or ""
            if not raw:
                continue
            if self._guard.is_whole_machine_brand(raw, brand_value=detected_brand):
                return True
        return False

    # ══════════════════════════════════════════════════════════
    #  公共入口
    # ══════════════════════════════════════════════════════════

    def judge(
        self,
        record: DeclarationRecord,
        ocr_texts: list[OcrText] | None = None,
    ) -> CheckResult:
        """对一条申报记录做四类判定（**纯函数，无副作用**）。

        判定主流程：

          1. **图片证据闸门**：``record.evidences`` 无有效图片 → ``NO_IMAGE``（🔵）。
             * 共享根不可达（``unreachable=True``）→ 仍判 ``NO_IMAGE``，
               但置 ``CheckResult.reason`` 标注"不可达"，供 T05 计连续 K 条暂停。
             * 目录不存在（``not_found=True``）→ 判 ``NO_IMAGE``，正常继续。
          2. **品牌比对**：SOP 3.4 规则①②③ → ``PASS`` / ``FAIL``。
          3. **型号比对**：同 ②③，不一致时用 ``diff_util`` 输出逐字符差异。
          4. **降级护栏**：任一字段 ``SUSPICIOUS`` → 整个记录强制 ``NO_MARK``（⚠️）。
          5. **外箱整机品牌**：命中的字段**保留异常**（不降级为合格）。

        Args:
            record: 申报记录（提供 ``decl_brand`` / ``decl_model`` / ``evidences``）。
            ocr_texts: 该记录的 OCR 结果列表；``None`` 时从 ``record.evidences[*].ocr`` 取。

        Returns:
            :class:`core.models.CheckResult`（``key`` 与 ``record.key()`` 一致）。
        """
        texts = self._resolve_ocr_texts(record, ocr_texts)
        key = record.key() if record is not None else ""

        result = CheckResult(key=key, record=record)
        result.evidence_images = list(record.evidences) if record is not None else []
        result.image_paths = ";".join(
            ev.image_path for ev in result.evidence_images if ev.image_path
        )
        result.evidence_text = self._build_evidence_snippet(texts)

        # ── ① 图片证据闸门 ──
        gate = self._check_image_gate(record)
        if gate is not None:
            gate.key = key
            gate.record = record
            gate.evidence_text = result.evidence_text
            gate.image_paths = result.image_paths
            gate.evidence_images = result.evidence_images
            return gate

        # ── ②③ 品牌 / 型号取证与比对 ──
        declared_brand = _normalize(record.decl_brand if record is not None else "")
        declared_model = _normalize(record.decl_model if record is not None else "")

        # 图片侧「同类标识」判据 + 展示值（v0.3.0：降级为**辅助**，不再是取证主路径）。
        #   * 用途 1：两级分流 —— 命中失败时判断"图内到底有没有同类标识"；
        #   * 用途 2：整机品牌上下文判定（需 detected_brand 作定位键）；
        #   * 用途 3：13 列汇总表「图片识别品牌/型号」列与旧结果集回退展示。
        detected_brand = extract_detected_brand(texts, self._repo)
        detected_model = extract_detected_model(texts)
        result.detected_brand = detected_brand
        result.detected_model = detected_model

        # ★ v0.3.0 主取证：申报值是否在 OCR 全文里以**完整分词**出现。
        matcher = TokenMatcher(texts, rules=self._repo)
        brand_match = matcher.match(declared_brand, field=_FIELD_BRAND)
        model_match = matcher.match(declared_model, field=_FIELD_MODEL)
        result.token_matches = [brand_match, model_match]

        # 整机上下文判定：**逐图**判定（缺陷 B 约束：检索范围限定在同一张图内）——
        # 任一图命中即视为整机品牌。绝不把不同图的行拼接后检索（防跨图污染）。
        whole_brand = self._is_whole_machine_any_image(texts, detected_brand)

        brand_verdict = self._guard.classify_detail(
            declared_brand,
            detected_brand,
            ocr_texts=texts,
            field_name=_FIELD_BRAND,
            whole_machine=whole_brand,
        )
        model_verdict = self._guard.classify_detail(
            declared_model,
            detected_model,
            ocr_texts=texts,
            field_name=_FIELD_MODEL,
            whole_machine=whole_brand,
        )

        # 记录级噪声兜底：**跨文字体系**比对（中↔英，如 宇同 ↔ YUTONG）
        # → 强制 SUSPICIOUS（⚠️），**绝不 ❌**（不虚高红线，SOP A.4）。
        #
        # ⚠️ 裁决 2 变更（架构裁决文档 §4.6）：原"整段命中 `known_noise_samples`
        # → 记录级判噪"的兜底**已撤销**——已知 OCR 误读改由**字段级自动纠正后比对**
        # 处理（`NoiseGuard.classify_detail` 命中样本 → 取 `expected_actual` 纠正 →
        # 再与申报值比对：一致 ✅ / 不一致 ❌）。若此处仍把"整段含已知噪声样本"判
        # SUSPICIOUS，会把纠正后本应判 ❌ 的记录（如 seq=16 `SKYWORTH`≠`DAEWOO`）
        # 错误降级为 ⚠️，与裁决 2 冲突。故**记录级仅保留跨文字体系兜底**。
        record_level_noise = self._detect_record_level_noise(
            texts, declared_brand, declared_model, detected_brand, detected_model
        )
        if record_level_noise and (
            brand_verdict.level != NoiseLevel.SUSPICIOUS
            and model_verdict.level != NoiseLevel.SUSPICIOUS
        ):
            target = self._pick_noise_target(f"{declared_brand}", f"{declared_model}")
            # ⚠️ v0.3.0：**已完整分词命中的字段不受记录级兜底影响**
            #   —— 命中是"直接证据"（申报值确实出现在 OCR 全文里），
            #      优先于"跨文字体系"这类**近似推断**；否则新口径下
            #      「申报『宇同』+ 图内出现『宇同』」会被兜底误降级为 ⚠️。
            if not brand_match.hit and (
                brand_verdict.level == NoiseLevel.CLEAR_MISMATCH or not detected_brand
            ):
                brand_verdict.level = NoiseLevel.SUSPICIOUS
                brand_verdict.signals.append("record_level_noise")
                brand_verdict.reason = (
                    f"{_FIELD_BRAND}：整段 OCR 命中已知噪声样本 / 存在跨文字体系比对，"
                    "疑似整体误读，转人工复核"
                )
            if not model_match.hit and (
                model_verdict.level == NoiseLevel.CLEAR_MISMATCH or not detected_model
            ):
                model_verdict.level = NoiseLevel.SUSPICIOUS
                model_verdict.signals.append("record_level_noise")
                if not model_verdict.reason or target:
                    model_verdict.reason = (
                        f"{_FIELD_MODEL}：整段 OCR 命中已知噪声样本 / 存在跨文字体系比对，"
                        "疑似整体误读，转人工复核"
                    )

        verdict, differences, noise_level, reason = self._decide(
            declared_brand,
            detected_brand,
            brand_verdict,
            declared_model,
            detected_model,
            model_verdict,
            brand_match=brand_match,
            model_match=model_match,
        )

        result.verdict = verdict
        result.differences = differences
        result.noise_level = noise_level
        result.reason = reason

        # ── ④ 降级护栏：SUSPICIOUS 绝不直达 ❌（不虚高红线）──
        self._apply_degrade_guard(result)

        return result

    # ══════════════════════════════════════════════════════════
    #  内部实现
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _resolve_ocr_texts(
        record: DeclarationRecord | None,
        ocr_texts: list[OcrText] | None,
    ) -> list[OcrText]:
        """解析本次比对的 OCR 文本列表（显式入参优先，否则从证据里取）。"""
        if ocr_texts is not None:
            return [t for t in ocr_texts if t is not None]
        if record is None:
            return []
        texts: list[OcrText] = []
        for evidence in record.evidences:
            if evidence is None or evidence.ocr is None:
                continue
            texts.append(evidence.ocr)
        return texts

    def _detect_record_level_noise(
        self,
        texts: list[OcrText],
        declared_brand: str,
        declared_model: str,
        detected_brand: str,
        detected_model: str,
    ) -> bool:
        """记录级噪声检测：**跨文字体系**比对（中 ↔ 拉丁）。

        这些信号无法在"单字段单值"层面被发现（如 ``宇同`` ↔ ``YUTONG`` 字符集不相交，
        逐字符比对不可信），故须在记录级兜底。

        ⚠️ **裁决 2 变更（架构裁决文档 §4.6）**：原"整段 OCR 命中已知噪声样本
        （``known_noise_samples``）→ 记录级判噪"的分支**已撤销**——已知 OCR 误读
        改由**字段级自动纠正后比对**处理（见
        :meth:`core.noise_guard.NoiseGuard.classify_detail`）。保留该分支会与
        裁决 2 冲突（把纠正后应判 ❌ 的记录错误降级为 ⚠️）。故此处**仅**保留
        跨文字体系兜底。

        Args:
            texts: OCR 结果列表（保留参数以兼容签名；当前不再用于样本匹配）。
            declared_brand: 申报品牌。
            declared_model: 申报型号。
            detected_brand: 图片识别品牌。
            detected_model: 图片识别型号。

        Returns:
            ``True`` 表示存在记录级噪声信号（应判 ``SUSPICIOUS``）。
        """
        del texts  # 裁决 2：不再做"整段命中已知噪声样本"的记录级判噪
        # 跨文字体系比对（中文品牌 ↔ OCR 拼音/英文，如 宇同 ↔ YUTONG）
        if self._guard.is_cross_script(declared_brand, detected_brand):
            return True
        if self._guard.is_cross_script(declared_model, detected_model):
            return True
        return False

    @staticmethod
    def _pick_noise_target(declared_brand: str, declared_model: str) -> bool:
        """辅助占位（保留以支持未来细分），当前返回是否至少一方有值。"""
        return bool(declared_brand or declared_model)

    def _check_image_gate(self, record: DeclarationRecord | None) -> CheckResult | None:
        """图片证据闸门：无有效图片 → ``NO_IMAGE``（🔵）。

        判定规则（架构设计 13.13.4，**钉死**）：

        ====================================  ============  ==================
        场景                                  verdict       是否置 unreachable
        ====================================  ============  ==================
        证据列表为空 / 全为 ``not_found``      ``NO_IMAGE``  **否**（正常继续）
        证据含 ``unreachable==True``          ``NO_IMAGE``  **是**（T05 计 K 条）
        ====================================  ============  ==================

        ⚠️ ``unreachable`` 与 ``not_found`` **严格互斥**：
        目录不存在（``not_found``）只判 🔵，**绝不置** ``unreachable``；
        否则用户票号填错会导致 T05 误挂整批。

        Args:
            record: 申报记录。

        Returns:
            ``CheckResult``（需判 ``NO_IMAGE``）或 ``None``（有有效图片，继续比对）。
        """
        evidences: list[ImageEvidence] = (
            list(record.evidences) if record is not None else []
        )
        if not evidences:
            return self._no_image_result("未找到对应图片（共享目录无该票号/料号/订单）")

        has_existing = any(ev.exists for ev in evidences)
        has_unreachable = any(ev.unreachable for ev in evidences)
        all_not_found = all(ev.not_found for ev in evidences)

        if has_unreachable:
            self._log.warning("共享盘不可达，该条判缺图（供上层连续计数触发暂停）")
            result = self._no_image_result("共享盘不可达（断连或权限不足），无法读取图片")
            result.noise_level = NoiseLevel.DEFINITE_MATCH
            # 结构化暂停标志位（T05 唯一判据）；not_found 路径**不置位**
            result.unreachable = True
            return result

        if not has_existing and (all_not_found or True):
            # 无任何存在的图片：目录不存在（not_found）或仅占位证据
            # ⚠️ 此处**不得**置 ``unreachable``（互斥红线，架构 13.13.4）
            return self._no_image_result("共享目录中不存在对应图片文件")

        # 有存在图片 → 不做闸门，继续比对
        return None

    @staticmethod
    def _no_image_result(reason: str) -> CheckResult:
        """构造 ``NO_IMAGE``（🔵）结果。"""
        return CheckResult(
            verdict=Verdict.NO_IMAGE,
            noise_level=NoiseLevel.DEFINITE_MATCH,
            reason=reason,
        )

    def _decide(
        self,
        declared_brand: str,
        detected_brand: str,
        brand_verdict: Any,
        declared_model: str,
        detected_model: str,
        model_verdict: Any,
        brand_match: TokenMatch | None = None,
        model_match: TokenMatch | None = None,
    ) -> tuple[Verdict, list[DifferenceDetail], NoiseLevel, str]:
        """按 SOP 1.3 / 3.4 生成四类判定 + 差异明细。

        **SOP 3.4 比对 5 条 → 结论映射（v0.3.0 逐条实现）**：

        ============================================  ==========================
        字段状态                                      对整条记录结论的贡献
        ============================================  ==========================
        双方均为无/空                                 → 合格
        申报值在图中以完整分词出现（EXACT/FUZZY）      → 合格
        ``SUSPICIOUS``（疑罪从无）                    → ⚠️（绝不 ❌）
        图内无**同类标识**（两级分流·无证据）          → ⚠️ 缺图内标识（**非 ❌**）
        图内有同类标识但值不同（两级分流·有证据）      → ❌ 校验异常
        申报缺失但图片明确有                          → ❌ 校验异常
        ============================================  ==========================

        整条记录取"最严重"者：❌ > ⚠️ > ✅。

        Args:
            declared_brand: 申报品牌。
            detected_brand: 图片识别品牌（v0.3.0：两级分流的"同类标识"判据）。
            brand_verdict: 品牌字段的 :class:`core.noise_guard.NoiseVerdict`。
            declared_model: 申报型号。
            detected_model: 图片识别型号。
            model_verdict: 型号字段的 :class:`core.noise_guard.NoiseVerdict`。
            brand_match: 品牌完整分词匹配结果（v0.3.0 主取证）。
            model_match: 型号完整分词匹配结果。

        Returns:
            ``(verdict, differences, noise_level, reason)``。
        """
        differences: list[DifferenceDetail] = []
        reasons: list[str] = []

        brand_state = self._field_state_v03(
            declared_brand, detected_brand, brand_verdict, brand_match
        )
        model_state = self._field_state_v03(
            declared_model, detected_model, model_verdict, model_match
        )

        # ── 差异明细（不一致 / 缺失时写；型号必须带逐字符差异）──
        if brand_state in (_FIELD_MISMATCH, _FIELD_SUSPICIOUS, _FIELD_DECLARED_MISSING):
            note = brand_verdict.reason if brand_verdict is not None else ""
            if brand_verdict is not None and brand_verdict.is_whole_machine:
                note = "外箱整机品牌，不构成本体证据"
            differences.append(
                DifferenceDetail(
                    field=_FIELD_BRAND,
                    declared_value=declared_brand,
                    detected_value=detected_brand,
                    char_diffs=char_diff(declared_brand, detected_brand),
                    note=note,
                )
            )
        elif brand_state == _FIELD_DETECTED_MISSING and brand_verdict is not None and (
            brand_verdict.is_whole_machine
        ):
            # 外箱整机品牌：作为差异记录保留（不构成本体证据），但不单独改判
            differences.append(
                DifferenceDetail(
                    field=_FIELD_BRAND,
                    declared_value=declared_brand,
                    detected_value=detected_brand,
                    char_diffs=char_diff(declared_brand, detected_brand),
                    note="外箱整机品牌，不构成本体证据",
                )
            )

        if model_state in (_FIELD_MISMATCH, _FIELD_SUSPICIOUS, _FIELD_DECLARED_MISSING):
            differences.append(
                DifferenceDetail(
                    field=_FIELD_MODEL,
                    declared_value=declared_model,
                    detected_value=detected_model,
                    char_diffs=char_diff(declared_model, detected_model),
                    note=model_verdict.reason if model_verdict else "",
                )
            )

        # ── 噪声三态：取双方"最可疑"者 ──
        noise_level = self._merge_noise_level(brand_verdict, model_verdict)

        # ── 整条记录结论：取最严重者 ──
        verdict = self._worst_verdict(brand_state, model_state)

        if verdict == Verdict.FAIL:
            if brand_state in (_FIELD_MISMATCH, _FIELD_DECLARED_MISSING) and brand_verdict is not None:
                reasons.append(brand_verdict.reason)
            if model_state in (_FIELD_MISMATCH, _FIELD_DECLARED_MISSING) and model_verdict is not None:
                reasons.append(model_verdict.reason)
            if not reasons:
                reasons.append("申报值与图片识别值不一致")
        elif verdict == Verdict.NO_MARK:
            if NoiseLevel.SUSPICIOUS in (brand_state, model_state):
                reasons.append("疑似 OCR 噪声（疑罪从无），转人工复核")
            if brand_state == _FIELD_SUSPICIOUS and brand_verdict is not None:
                reasons.append(brand_verdict.reason)
            if model_state == _FIELD_SUSPICIOUS and model_verdict is not None:
                reasons.append(model_verdict.reason)
            if brand_state == _FIELD_DETECTED_MISSING or model_state == _FIELD_DETECTED_MISSING:
                reasons.append("图片内未识别到品牌/型号文字，转人工复核")
            if not reasons:
                reasons.append("证据不足，转人工复核")
        else:  # PASS
            if brand_state == _FIELD_BOTH_ABSENT and model_state == _FIELD_BOTH_ABSENT:
                reasons.append("品牌与型号双方均为『无』，判合格")
            elif brand_state == _FIELD_BOTH_ABSENT:
                reasons.append("品牌双方均为『无』；型号一致，判合格")
            elif model_state == _FIELD_BOTH_ABSENT:
                reasons.append("型号双方均为『无』；品牌一致，判合格")
            else:
                reasons.append("申报值与图片识别值一致，判合格")

        # ── v0.3.0 可追溯：附上完整分词匹配的取证说明（✅/⚠️ 均写，❌ 由差异明细承载）──
        reasons.extend(
            _match_reasons(brand_state, brand_match, _FIELD_BRAND)
        )
        reasons.extend(
            _match_reasons(model_state, model_match, _FIELD_MODEL)
        )

        return verdict, differences, noise_level, "；".join(r for r in reasons if r)

    @classmethod
    def _field_state_v03(
        cls,
        declared: str,
        detected: str,
        verdict: Any,
        match: TokenMatch | None = None,
    ) -> str:
        """判定单字段状态（**v0.3.0 口径**：完整分词命中为主取证）。

        判定顺序（顺序本身即"不虚高"的实现）：

          1. **申报侧为「无」/空** → 沿用旧链路 :meth:`_field_state`
             （规则①「双方均为无 → ✅」/ 规则④「申报无但图内有 → ❌」语义**不变**）；
          2. **完整分词命中**（``EXACT`` / ``FUZZY``）→ ``MATCH``（✅）；
          3. **未命中 且 疑似噪声** → ``SUSPICIOUS``（⚠️，**绝不 ❌**）；
          4. **未命中 但 申报值与图片识别值一致**（旧链路等价证据，兼容保留）→ ``MATCH``；
          5. **两级分流**：图内**有同类标识**但值不同 → ``MISMATCH``（❌，有证据）；
             图内**根本没有**该类标识 → ``DETECTED_MISSING``（⚠️，没找到证据）。
             —— 「有证据不一致」与「没找到证据」不可混淆（SOP 1.3 红线）。

        Args:
            declared: 申报值。
            detected: 图片识别值（v0.3.0：作为"图内是否有同类标识"的判据）。
            verdict: 字段的 :class:`core.noise_guard.NoiseVerdict`（未命中时的兜底三态）。
            match: 完整分词匹配结果；``None`` 时等价于未命中。

        Returns:
            字段状态常量之一（:data:`_FIELD_*`）。
        """
        # ① 申报侧为「无」→ 旧链路语义（规则① / 规则④）完全不变
        if is_none_token(declared):
            return cls._field_state(declared, detected, verdict)

        # ② 完整分词命中 → 合格（v0.3.0 主路径）
        if match is not None and match.hit:
            return _FIELD_MATCH

        # ③ 未命中但疑似噪声 → ⚠️（不虚高红线优先于"未命中即异常"）
        level = getattr(verdict, "level", NoiseLevel.DEFINITE_MATCH)
        if level == NoiseLevel.SUSPICIOUS:
            return _FIELD_SUSPICIOUS

        detected_absent = is_none_token(detected)

        # ④ 兼容：申报值与图片侧识别值一致（旧链路的等价证据）→ 合格
        if level == NoiseLevel.DEFINITE_MATCH and not detected_absent:
            return _FIELD_MATCH

        # ⑤ 两级分流
        if not detected_absent:
            return _FIELD_MISMATCH
        return _FIELD_DETECTED_MISSING

    @staticmethod
    def _field_state(declared: str, detected: str, verdict: Any) -> str:
        """判定单字段状态（SOP 3.4 规则①②③④ 的字段级落点）。

        ⚠️ v0.3.0 起本方法**只在"申报侧为无/空"时**由
        :meth:`_field_state_v03` 调用（规则①「双方均为无 → ✅」与规则④
        「申报缺失但图片明确有 → ❌」的语义**完全不变**）；申报侧有值时的取证
        已改走「完整分词命中」，见 :meth:`_field_state_v03`。

        Args:
            declared: 申报值。
            detected: 图片识别值。
            verdict: 字段的 :class:`core.noise_guard.NoiseVerdict`。

        Returns:
            字段状态常量之一（:data:`_FIELD_*`）。
        """
        if verdict is not None and verdict.both_absent:
            return _FIELD_BOTH_ABSENT
        level = getattr(verdict, "level", NoiseLevel.DEFINITE_MATCH)
        if level == NoiseLevel.SUSPICIOUS:
            return _FIELD_SUSPICIOUS
        if level == NoiseLevel.DEFINITE_MATCH:
            return _FIELD_MATCH

        declared_absent = is_none_token(declared)
        detected_absent = is_none_token(detected)

        # 申报缺失但图片明确有 → ❌（SOP 3.4 规则④）
        if declared_absent and not detected_absent:
            return _FIELD_DECLARED_MISSING
        # 图片侧无标识（申报有值，但图内无该文字）→ ⚠️ 缺图内标识（**非 ❌**）
        if detected_absent:
            return _FIELD_DETECTED_MISSING
        # 双方有值但不一致 → ❌（SOP 3.4 规则③）
        return _FIELD_MISMATCH

    @staticmethod
    def _worst_verdict(brand_state: str, model_state: str) -> Verdict:
        """按"最严重者优先"合并两字段状态为整条记录结论（❌ > ⚠️ > ✅）。"""
        states = (brand_state, model_state)
        if _FIELD_MISMATCH in states or _FIELD_DECLARED_MISSING in states:
            return Verdict.FAIL
        if _FIELD_SUSPICIOUS in states or _FIELD_DETECTED_MISSING in states:
            return Verdict.NO_MARK
        return Verdict.PASS

    @staticmethod
    def _merge_noise_level(brand_verdict: Any, model_verdict: Any) -> NoiseLevel:
        """合并两字段的噪声三态（``SUSPICIOUS`` 优先级最高）。"""
        levels = [
            getattr(v, "level", NoiseLevel.DEFINITE_MATCH)
            for v in (brand_verdict, model_verdict)
            if v is not None
        ]
        if NoiseLevel.SUSPICIOUS in levels:
            return NoiseLevel.SUSPICIOUS
        if NoiseLevel.CLEAR_MISMATCH in levels:
            return NoiseLevel.CLEAR_MISMATCH
        return NoiseLevel.DEFINITE_MATCH

    def _apply_degrade_guard(self, result: CheckResult) -> None:
        """**不虚高红线**：``SUSPICIOUS`` → 强制 ``NO_MARK``（⚠️），绝不允许 ``FAIL``。

        本方法是红线的**唯一兜底闸门**：无论上游分支如何，只要 ``noise_level`` 为
        ``SUSPICIOUS`` 而 ``verdict`` 为 ``FAIL``，一律改写为 ``NO_MARK`` 并附说明。

        Args:
            result: 待处置的 :class:`core.models.CheckResult`（原地修改）。
        """
        if result.verdict == Verdict.FAIL and result.noise_level == NoiseLevel.SUSPICIOUS:
            result.verdict = Verdict.NO_MARK
            note = "疑似 OCR 噪声（不虚高红线：疑罪从无），已降级为 ⚠️ 待人工复核"
            if note not in result.reason:
                result.reason = f"{result.reason}；{note}" if result.reason else note

        # 外箱整机品牌命中的记录：保留异常（❌）不降级；若因噪声降级则保持 ⚠️。
        # 该分支只做"确保 ❌ 未被误改成合格"的保护（防御式，正常路径不会触发）。
        if result.verdict == Verdict.PASS and result.differences:
            has_preserved_fail = any(
                d.note and "整机品牌" in d.note for d in result.differences
            )
            if has_preserved_fail:
                # 绝不把"外箱整机品牌"造成的差异静默改判为合格
                result.verdict = Verdict.FAIL
                extra = "外箱整机品牌与申报值不一致，保留异常（不构成本体证据但需人工确认）"
                if extra not in result.reason:
                    result.reason = f"{result.reason}；{extra}" if result.reason else extra

    @staticmethod
    def _build_evidence_snippet(texts: list[OcrText], limit: int = 120) -> str:
        """构造「证据（OCR 片段）」列内容。

        ⚠️ **完整 OCR 原文只进 JSON 证据文件，不进日志**（架构设计 9.3）。
        本列的片段供汇总表阅读；截断长度上限 120 字符。

        Args:
            texts: OCR 结果列表。
            limit: 片段字符上限。

        Returns:
            以 `` / `` 连接的 OCR 片段（超长截断）。
        """
        parts: list[str] = []
        for text in texts:
            raw = (getattr(text, "text_raw", "") or "").strip()
            if not raw:
                continue
            compact = " ".join(raw.split())
            if len(compact) > limit:
                compact = compact[:limit] + "…"
            parts.append(compact)
            if len(parts) >= 3:
                break
        return " / ".join(parts)


#: 供上层直接引用的口径常量（避免硬编码）
VERDICT_LABELS: dict[Verdict, str] = constants.VERDICT_LABELS
