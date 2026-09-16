"""缺陷 C 回归锚点（``tests/test_brand_extraction.py``，T06）。

覆盖架构裁决文档 §4.5 的「跨图一致性投票 + 分层优先级 + 标签图加权」四个锚点，
以及 §6.2「字段名残片不得作本体品牌证据」的补充锚点。
"""

from __future__ import annotations

from core.judge_engine import (
    _is_field_name_fragment,
    _load_field_name_terms,
    extract_detected_brand,
)
from core.models import OcrText


def _ocr(text: str, *, image: str, confidence: float = 0.95) -> OcrText:
    return OcrText(
        image_path=image,
        text_raw=text,
        confidence=confidence,
        boxes=[(0, 0, 10, 10)],
        seq=0,
        low_confidence=False,
    )


# ══════════════════════════════════════════════════════════════════
#  §4.5 锚点 1：真实 seq=11 形态 —— 单图噪声 vs 多图一致
# ══════════════════════════════════════════════════════════════════
def test_real_seq11_shape_picks_daewoo(rule_repo) -> None:
    """真实 seq=11 形态：img1='ONEL'(单图) … img9/10/11='Daewoo' → 取 Daewoo。

    依据：§4.5 锚点 1。
    """
    texts = [
        _ocr("ONEL\nCLAB\nMODEL:A9KB9G", image="/img/&001.jpg"),   # 单图噪声
        _ocr("WLO\nC7J23TN1J1", image="/img/&002.jpg"),
        _ocr("Brand:Daewoo\nJOB NO.:2660326M", image="/img/&009.jpg"),
        _ocr("Brand:Daewoo\nJOB NO.:2660326M", image="/img/&010.jpg"),
        _ocr("Brand:Daewoo\nJOB NO.:2660326M", image="/img/&011.jpg"),
    ]
    assert extract_detected_brand(texts, rule_repo) == "Daewoo"


# ══════════════════════════════════════════════════════════════════
#  §4.5 锚点 2：单图孤例噪声被拒
# ══════════════════════════════════════════════════════════════════
def test_single_image_noise_rejected(rule_repo) -> None:
    """裸 token 单图孤例（``ONEL``）→ 不采信，返回空串。

    依据：§4.5 锚点 2 / §4.2（裸 token 层须 ≥2 图一致）。
    """
    texts = [_ocr("ONEL", image="/img/&001.jpg")]
    assert extract_detected_brand(texts, rule_repo) == ""


def test_bare_token_two_images_accepted(rule_repo) -> None:
    """裸 token 在 ≥2 张不同图出现 → 采信。"""
    texts = [
        _ocr("COOCAA", image="/img/&001.jpg"),
        _ocr("COOCAA", image="/img/&002.jpg"),
    ]
    assert extract_detected_brand(texts, rule_repo) == "COOCAA"


# ══════════════════════════════════════════════════════════════════
#  §4.5 锚点 3：分层优先级（显式标签 > 裸 token）
# ══════════════════════════════════════════════════════════════════
def test_explicit_label_beats_bare_token(rule_repo) -> None:
    """显式 ``Brand:Daewoo`` 存在时，不再让 ``ONEL`` 等裸 token 干扰。

    依据：§4.5 锚点 3 / §4.2 分层优先级。
    """
    texts = [
        _ocr("ONEL\nCLAB", image="/img/&001.jpg"),
        _ocr("Brand:Daewoo\nJOB NO.:1", image="/img/&009.jpg"),
    ]
    assert extract_detected_brand(texts, rule_repo) == "Daewoo"


# ══════════════════════════════════════════════════════════════════
#  §4.5 锚点 4：标签图加权（&001 权重 ×2）
# ══════════════════════════════════════════════════════════════════
def test_label_image_weight_breaks_tie(rule_repo) -> None:
    """标签图 ``&001`` 候选权重 ×2，平票时胜出。

    依据：§4.5 锚点 4 / §4.3（``&001`` 标签图加权）。
    构造：``FOOBAR`` 出现在 1 张标签图 + 1 张普通图 → 权重 2+1=3；
          ``BAZQUX`` 出现在 2 张普通图 → 权重 1+1=2。两者均满足 ≥2 图，
          ``FOOBAR`` 因标签图加权胜出。
    """
    texts = [
        _ocr("FOOBAR", image="/img/&001.jpg"),   # 标签图（权重 2）
        _ocr("FOOBAR", image="/img/&002.jpg"),   # 普通图（权重 1）→ FOOBAR 合计 3
        _ocr("BAZQUX", image="/img/&003.jpg"),   # 普通图（权重 1）
        _ocr("BAZQUX", image="/img/&004.jpg"),   # 普通图（权重 1）→ BAZQUX 合计 2
    ]
    assert extract_detected_brand(texts, rule_repo) == "FOOBAR"


# ══════════════════════════════════════════════════════════════════
#  §6.2 补充锚点：字段名残片不得作本体品牌证据
# ══════════════════════════════════════════════════════════════════
def test_field_name_fragment_rejected(rule_repo) -> None:
    """``MFR P/N`` / ``MWFR P/N`` 等字段名残片 → 不作品牌证据（口径 2）。

    依据：§6.2「字段名残片（``SKYWORTH P/N``）不算『图片明确有』」。
    实测 seq=8/16 曾因 ``MFR`` / ``MWFR P`` 被判 ❌，此为回归锚点。
    """
    terms = _load_field_name_terms(rule_repo)
    assert _is_field_name_fragment("MFR", terms)
    assert _is_field_name_fragment("MWFR", terms)
    assert _is_field_name_fragment("Manufacturer", terms)
    assert _is_field_name_fragment("IFR", terms)
    # 真品牌不得被误杀
    assert not _is_field_name_fragment("Daewoo", terms)
    assert not _is_field_name_fragment("SANSUI", terms)


def test_mfr_pn_line_yields_no_brand(rule_repo) -> None:
    """仅 ``MFR P/N`` 行（字段名）→ 识别值置空。"""
    texts = [_ocr("MFR P/N\nN011901-007957-001", image="/img/&001.jpg")]
    assert extract_detected_brand(texts, rule_repo) == ""


def test_skyhorth_pn_line_yields_skyhorth(rule_repo) -> None:
    """``SKYHORTH P/H``（应为 ``SKYWORTH P/N``）→ 品牌前缀 ``SKYHORTH``（供噪声比对）。

    依据：§8.1 seq=16 —— ``SKYHORTH`` 是 ``SKYWORTH`` 的 OCR 误读，须被提取以便
    噪声护栏判 ⚠️（**非** 字段名残片）。
    """
    texts = [_ocr("SKYHORTH P/H\n创维物料编号", image="/img/&001.jpg")]
    assert extract_detected_brand(texts, rule_repo) == "SKYHORTH"
