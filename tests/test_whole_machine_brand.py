"""缺陷 A / B 回归锚点（``tests/test_whole_machine_brand.py``，T06）。

覆盖架构裁决文档：

  * §1.4 —— 缺陷 A：``Customer model`` 单独出现（无 ``CARTON``/``JOB NO.``）
    **不得**判整机品牌；``CARTON No.`` / ``JOB NO.`` + ``Brand:`` 组合仍正确命中。
  * §3.4 —— 缺陷 B：``context_scope == "whole_image"`` 时整机上下文按**整图全文**共现
    （不限行距），且命中范围**限定在同一张图内**（不跨图拼接）。
  * §2.5 —— 整机品牌命中 + 申报无品牌 → 保留 ❌（SOP 3.4 第 147 行）。
"""

from __future__ import annotations

from core.judge_engine import JudgeEngine
from core.models import DeclarationRecord, ImageEvidence, OcrText, Verdict
from core.noise_guard import NoiseGuard


def _ocr(text: str, *, image: str = "/img/&001.jpg", confidence: float = 0.95) -> OcrText:
    return OcrText(
        image_path=image,
        text_raw=text,
        confidence=confidence,
        boxes=[(0, 0, 10, 10)],
        seq=1,
        low_confidence=False,
    )


def _evidence(text: str, *, image: str = "/img/&001.jpg") -> ImageEvidence:
    return ImageEvidence(
        image_path=image,
        seq=1,
        exists=True,
        ocr_confidence=0.95,
        ocr=_ocr(text, image=image),
    )


# ══════════════════════════════════════════════════════════════════
#  缺陷 A：Customer model 移出整机上下文
# ══════════════════════════════════════════════════════════════════
def test_customer_model_alone_is_not_whole_machine(rule_repo) -> None:
    """``Customer model`` 单独出现（无 ``CARTON``/``JOB NO.``）→ 不得判整机品牌。

    依据：§1.4 锚点。缺陷 A 裁决把 ``Customer model`` 从 ``context_tokens`` 移出。
    """
    guard = NoiseGuard(rule_repo)
    text = "Brand:SANSUI\nCustomer model:SAN-25GT32\nProduct Name\nCRIBS"
    assert not guard.is_whole_machine_brand(text, brand_value="SANSUI"), (
        "Customer model 单独出现不得命中整机上下文（缺陷 A 回归锚点）"
    )


def test_carton_or_jobno_with_brand_is_whole_machine(rule_repo) -> None:
    """``CARTON No.`` / ``JOB NO.`` + ``Brand:`` 组合 → 仍正确判整机品牌（不误伤）。

    依据：§1.4 锚点（缺陷 A 修复不得误伤真整机命中）。
    """
    guard = NoiseGuard(rule_repo)
    assert guard.is_whole_machine_brand(
        "Brand:Daewoo\nJOB NO.:2660328M\nCARTON No.:A2", brand_value="Daewoo"
    )
    assert guard.is_whole_machine_brand(
        "Brand:SANSUI\nJOB NO.:2660328M\nCARTON No.:A2", brand_value="SANSUI"
    )


# ══════════════════════════════════════════════════════════════════
#  缺陷 B：整图全文检索（context_scope == whole_image）
# ══════════════════════════════════════════════════════════════════
def test_whole_image_scope_allows_far_apart_rows(rule_repo) -> None:
    """``context_scope=whole_image`` 时，``Brand:`` 与整机 token 即使相隔多行也命中。

    依据：§3.4 锚点。旧 ``context_window=3`` 会漏判远距共现；整图口径应命中。
    构造：``Brand:Daewoo`` 与 ``CARTON No.:A2`` 之间隔 8 行（超出 window=3）。
    """
    guard = NoiseGuard(rule_repo)
    lines = ["Brand:Daewoo"] + [f"filler-{i}" for i in range(8)] + ["CARTON No.:A2"]
    assert guard.is_whole_machine_brand("\n".join(lines), brand_value="Daewoo"), (
        "整图口径下远距共现应命中（缺陷 B 回归锚点）"
    )


def test_whole_image_scope_is_configured(rule_repo) -> None:
    """规则快照的 ``context_scope`` 为 ``whole_image``（缺陷 B 配置锚点）。"""
    assert rule_repo.get().whole_machine_brand.context_scope == "whole_image"


# ══════════════════════════════════════════════════════════════════
#  §2.5：整机品牌命中 + 申报无品牌 → 保留 ❌
# ══════════════════════════════════════════════════════════════════
def test_whole_machine_brand_preserves_fail(rule_repo) -> None:
    """整机品牌命中 + 申报无品牌 → ❌（SOP 3.4 第 147 行「字段缺失→❌」）。

    依据：§2.5 锚点。修复 A/B 后不得把整机品牌降级为 ⚠️。
    """
    engine = JudgeEngine(rules=rule_repo)
    record = DeclarationRecord(
        ticket_no="T",
        seq_no=9,
        decl_brand="",
        decl_model="L8M30E0",
        evidences=[_evidence("Brand:Daewoo\nJOB NO.:2660310M\nCARTON No.:A1")],
    )
    result = engine.judge(record)
    assert result.verdict == Verdict.FAIL
    assert any("整机品牌" in (d.note or "") for d in result.differences)


def test_whole_machine_branch_not_falsely_triggered(rule_repo) -> None:
    """缺陷 A 修复后：非整机图不得误命中整机分支（防范围错乱）。

    依据：§2.5 / T06-16R。仅供 ``Brand:`` 无 ``CARTON``/``JOB NO.`` → ``whole_machine=False``
    → 不得因整机分支判 ❌。
    """
    engine = JudgeEngine(rules=rule_repo)
    record = DeclarationRecord(
        ticket_no="T",
        seq_no=1,
        decl_brand="SANSUI",
        decl_model="",
        # 仅 Brand:SANSUI（与申报一致）但无 CARTON/JOB NO. → 非整机
        evidences=[_evidence("Brand:SANSUI\nProduct Name\nCRIBS")],
    )
    result = engine.judge(record)
    assert result.verdict != Verdict.FAIL, "非整机图不得因整机分支误判 ❌"


def test_customer_model_not_body_model(rule_repo) -> None:
    """缺陷 A / 口径 1：``Customer model`` 不作本体型号证据。

    ``Customer model:XXX`` 不得被 :func:`extract_detected_model` 提取为本体型号。
    """
    from core.judge_engine import extract_detected_model

    texts = [_ocr("Customer model:SAN-25GT32\nBrand:SANSUI")]
    assert extract_detected_model(texts) == "", (
        "Customer model 不得作本体型号证据（缺陷 A / 口径 1）"
    )
