"""T04 判定引擎回归测试（``tests/test_judge_engine.py``）。

覆盖架构设计第 7 节 T04 验收要点：

  * ① 四类口径（SOP 1.3 / 3.4）逐条用例
  * ② 「双方均为无/空 → 判合格」硬断言
  * ③ 型号不一致时**逐字符差异**输出（``diff_util``）
  * ④ **不虚高红线**：``NoiseGuard`` 判 ``SUSPICIOUS`` 的记录**绝不** ``Verdict.FAIL``
  * ⑤ ``IFR PIN`` / ``boori E339609`` / ``YUTONG`` 三类噪声落入 ⚠️
  * ⑥ 外箱整机品牌（``Brand:Daewoo`` + ``CARTON``/``JOBNOB``）**保留异常**
  * 验证 ``NO_IMAGE`` 的 ``unreachable`` / ``not_found`` 区分
  * 验证 ``JudgeEngine.judge`` 是纯函数（无副作用、可重复调用）
"""

from __future__ import annotations

import pytest

from core.constants import (
    ALL_VERDICTS,
    VERDICT_FAIL,
    VERDICT_NO_IMAGE,
    VERDICT_NO_MARK,
    VERDICT_PASS,
    verdict_text,
)
from core.diff_util import char_diff
from core.judge_engine import JudgeEngine, extract_detected_brand, extract_detected_model
from core.models import (
    DeclarationRecord,
    ImageEvidence,
    NoiseLevel,
    OcrText,
    Verdict,
)

# ══════════════════════════════════════════════════════════════════
#  测试辅助
# ══════════════════════════════════════════════════════════════════


def make_ocr(text: str, *, confidence: float = 0.95, low: bool = False, seq: int = 1) -> OcrText:
    """构造一条 OCR 结果。"""
    return OcrText(
        image_path=f"/img/{seq}.jpg",
        text_raw=text,
        confidence=confidence,
        boxes=[(0, 0, 10, 10)],
        seq=seq,
        low_confidence=low,
    )


def make_evidence(text: str, *, seq: int = 1, confidence: float = 0.95, low: bool = False) -> ImageEvidence:
    """构造一条含 OCR 的图片证据。"""
    return ImageEvidence(
        image_path=f"/img/{seq}.jpg",
        seq=seq,
        exists=True,
        ocr_confidence=confidence,
        ocr=make_ocr(text, confidence=confidence, low=low, seq=seq),
    )


def make_record(
    *,
    brand: str = "",
    model: str = "",
    evidences: list[ImageEvidence] | None = None,
    part_no: str = "N011901-007386-001",
    order_no: str = "2660326M",
    ticket_no: str = "SA26090215",
) -> DeclarationRecord:
    """构造一条申报记录。"""
    return DeclarationRecord(
        ticket_no=ticket_no,
        part_no=part_no,
        order_no=order_no,
        product_name="FFC线材",
        seq_no=1,
        raw_element_text=f"品牌:{brand}|型号:{model}",
        decl_brand=brand,
        decl_model=model,
        evidences=list(evidences or []),
    )


@pytest.fixture
def engine(rule_repo) -> JudgeEngine:
    """注入规则仓库的判定引擎。"""
    return JudgeEngine(rules=rule_repo)


# ══════════════════════════════════════════════════════════════════
#  ① 四类口径（SOP 1.3 / 3.4）
# ══════════════════════════════════════════════════════════════════


class TestFourVerdicts:
    """四类判定口径逐条用例。"""

    def test_pass_when_both_match(self, engine: JudgeEngine) -> None:
        """品牌 + 型号双方一致 → ✅ 校验合格。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:baori 型号:A7A01G")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert verdict_text(result.verdict) == VERDICT_PASS

    def test_fail_when_clear_mismatch(self, engine: JudgeEngine) -> None:
        """双方有值但明确不一致（差异巨大）→ ❌ 校验异常。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:SAMSUNG-LCD 型号:UA55NEOQ9000")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL
        assert verdict_text(result.verdict) == VERDICT_FAIL
        assert result.noise_level == NoiseLevel.CLEAR_MISMATCH

    def test_fail_when_declared_missing_but_image_has(self, engine: JudgeEngine) -> None:
        """申报缺失但图片明确有 → ❌（SOP 3.4 规则④）。"""
        record = make_record(
            brand="",
            model="",
            evidences=[make_evidence("品牌:baori 型号:A7A01G")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL

    def test_no_mark_when_image_has_no_text(self, engine: JudgeEngine) -> None:
        """图片存在但无品牌/型号文字 → ⚠️ 缺图内标识。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("数量 / 201 / 生产日期 / 20260707 / QTY")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_MARK
        assert verdict_text(result.verdict) == VERDICT_NO_MARK

    def test_no_image_when_no_evidence(self, engine: JudgeEngine) -> None:
        """无任何图片证据 → 🔵 缺图。"""
        record = make_record(brand="baori", model="A7A01G", evidences=[])
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_IMAGE
        assert verdict_text(result.verdict) == VERDICT_NO_IMAGE

    def test_case_only_difference_pass(self, engine: JudgeEngine) -> None:
        """仅大小写不同 → ✅（**不是噪声**，不得降级为 ⚠️）。

        回归锁：申报 ``DAEWOO`` vs 唛头 ``Daewoo`` 曾因易混字符归一化（内部 ``.upper()``）
        被误判为 ``SUSPICIOUS``。品牌大小写差异是同一取值，必须判合格。
        """
        record = make_record(
            brand="DAEWOO",
            model="",
            evidences=[make_evidence("Brand:Daewoo\nCARTON\nJOBNO")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert result.noise_level == NoiseLevel.DEFINITE_MATCH
        assert result.detected_brand.upper() == "DAEWOO"

    def test_all_verdicts_covered(self) -> None:
        """四类判定枚举与口径常量一一对应（口径红线）。"""
        assert set(ALL_VERDICTS) == {Verdict.PASS, Verdict.FAIL, Verdict.NO_MARK, Verdict.NO_IMAGE}


# ══════════════════════════════════════════════════════════════════
#  ② 双方均为无 / 空 → 判合格（硬断言）
# ══════════════════════════════════════════════════════════════════


class TestBothAbsent:
    """SOP 3.4 规则①：双方均为无/空 → ✅（硬断言）。"""

    def test_both_none_token_pass(self, engine: JudgeEngine) -> None:
        """申报『无』+ 图片『无品牌』→ ✅ 合格。"""
        record = make_record(
            brand="无",
            model="无",
            evidences=[make_evidence("品牌:无 型号:无")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert result.noise_level == NoiseLevel.DEFINITE_MATCH

    def test_both_empty_pass(self, engine: JudgeEngine) -> None:
        """申报空 + 图片无任何标识 → 但图片存在：判 ⚠️（因图片无有效字体）。

        说明：本用例验证"申报空 + 图片无标识"时**不得**判 ❌（不虚高 / 不误伤）。
        """
        record = make_record(brand="", model="", evidences=[make_evidence("数量201")])
        result = engine.judge(record)
        assert result.verdict != Verdict.FAIL
        assert result.verdict in (Verdict.NO_MARK, Verdict.PASS)

    def test_declared_none_image_absent_pass(self, engine: JudgeEngine) -> None:
        """申报『N/A』+ 图片『NONE』→ ✅ 合格（无值等价类）。"""
        record = make_record(
            brand="N/A",
            model="NONE",
            evidences=[make_evidence("品牌:NONE 型号:N/A")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS

    def test_both_absent_hard_assert(self, engine: JudgeEngine) -> None:
        """硬断言：双方均为"无"必须恰好是 PASS（SOP 3.4 规则①）。"""
        for declared, detected in [
            ("无", "无"),
            ("无品牌", "无"),
            ("", ""),
            ("NA", "N/A"),
            ("NONE", "空"),
        ]:
            record = make_record(
                brand=declared,
                model=declared,
                evidences=[make_evidence(f"品牌:{detected} 型号:{detected}")],
            )
            result = engine.judge(record)
            assert result.verdict == Verdict.PASS, f"{declared!r} vs {detected!r} 应判合格"


# ══════════════════════════════════════════════════════════════════
#  ③ 逐字符差异（diff_util）
# ══════════════════════════════════════════════════════════════════


class TestCharDiff:
    """SOP 3.4 规则⑤：型号不一致必须输出逐字符差异。"""

    def test_model_mismatch_has_char_diffs(self, engine: JudgeEngine) -> None:
        """型号不一致 → 差异明细必须含 ``char_diffs``（无论判定是 ⚠️ 还是 ❌）。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:baori 型号:A7A02G")],
        )
        result = engine.judge(record)
        model_diffs = [d for d in result.differences if d.field == "型号"]
        assert model_diffs, "型号不一致必须产出差异明细"
        assert model_diffs[0].char_diffs, "型号不一致必须列出逐字符差异"
        assert any("1" in d and "2" in d for d in model_diffs[0].char_diffs)

    def test_model_clear_mismatch_char_diffs(self, engine: JudgeEngine) -> None:
        """明确不一致的型号差异明细同样含逐字符差异。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:baori 型号:ZZZ99999")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL
        model_diffs = [d for d in result.differences if d.field == "型号"]
        assert model_diffs[0].char_diffs

    def test_char_diff_function(self) -> None:
        """``char_diff`` 基本行为。"""
        assert char_diff("A7A01G", "A7A02G") == ["『申报1』→『图片2』（第4位起）"]
        assert char_diff("abc", "abc") == []
        assert char_diff("abc", "abcd") == ["图片多出『d』（第3位起）"]
        assert char_diff("abcd", "abc") == ["申报多出『d』（第3位起）"]

    def test_char_diff_confusable_pair(self) -> None:
        """易混字符误读的逐字符差异必须体现 W→H。"""
        diffs = char_diff("SKYWORTH P/N", "SKYHORTH P/H")
        assert diffs, "误读必须列出差异点"
        joined = "".join(diffs)
        assert "W" in joined and "H" in joined


# ══════════════════════════════════════════════════════════════════
#  ④ 不虚高红线（硬断言）
# ══════════════════════════════════════════════════════════════════


class TestNoInflationRedline:
    """R5：``SUSPICIOUS`` 绝不直达 ``FAIL``。"""

    def test_confusable_noise_never_fail(self, engine: JudgeEngine) -> None:
        """**裁决 2（R4，T06-18）**：``SKYHORTH P/H`` 命中已知样本 → 纠正为
        ``SKYWORTH P/N`` → 与申报 ``SKYWORTH P/N`` **一致** → ✅。

        ⚠️ 原断言（"命中已知样本 → SUSPICIOUS / NO_MARK"）**已作废**（R4 裁决 2：
        否决"已知误读独立判噪"，改采"自动纠正后比对"）。本用例验证**纠正后一致
        判合格**；不一致场景见 ``test_known_misread_corrected_then_fail``。

        说明：仅申报品牌（型号留空）——纠正后品牌一致 + 型号双方均为"无" → ✅。
        """
        record = make_record(
            brand="SKYWORTH P/N",
            model="",
            evidences=[make_evidence("SKYHORTH P/H\n创维物料编号\nN011901-007957-001")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS, (
            f"裁决 2：SKYHORTH 纠正为 SKYWORTH 后与申报一致，应判 ✅，实际 {result.verdict}"
        )
        assert result.verdict != Verdict.FAIL, "纠正后一致，不得判 ❌"

    def test_known_misread_corrected_then_fail(self, engine: JudgeEngine) -> None:
        """**裁决 2（§4.6.3）**：``SKYHORTH`` → 纠正为 ``SKYWORTH`` → 与申报
        ``DAEWOO`` **不一致** → ❌（依据 = ``SKYWORTH`` ≠ ``DAEWOO``，更可审计）。"""
        record = make_record(
            brand="DAEWOO",
            model="",
            evidences=[make_evidence("SKYHORTH P/N")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL, (
            f"裁决 2：纠正后 SKYWORTH ≠ DAEWOO，应判 ❌，实际 {result.verdict}"
        )

    def test_suspicious_hard_assert_all_cases(self, engine: JudgeEngine) -> None:
        """**硬断言**：任何 ``noise_level == SUSPICIOUS`` 的记录 ``verdict != FAIL``。"""
        cases = [
            ("SKYWORTH P/N", "SKYHORTH P/H"),
            ("26090215ABC", "2609O215ABC"),      # O/0 误读
            ("MODEL-12345", "MODEL-1234S"),      # S/5 误读
            ("GXD-009", "GXD-00G"),              # 9/G 误读 + 模糊
        ]
        for declared, detected in cases:
            record = make_record(
                brand=declared,
                model=declared,
                evidences=[make_evidence(f"品牌:{detected} 型号:{detected}")],
            )
            result = engine.judge(record)
            if result.noise_level == NoiseLevel.SUSPICIOUS:
                assert result.verdict != Verdict.FAIL, (
                    f"违反不虚高红线：{declared!r} vs {detected!r} "
                    f"被判 {result.verdict}（应为 ⚠️）"
                )

    def test_low_confidence_is_suspicious_not_fail(self, engine: JudgeEngine) -> None:
        """低置信度 OCR → SUSPICIOUS（⚠️），不得 ❌。"""
        record = make_record(
            brand="SAMSUNG",
            model="UA55NEOQ",
            evidences=[make_evidence("品牌:PHILIPS 型号:XYZ999", confidence=0.2, low=True)],
        )
        result = engine.judge(record)
        assert result.noise_level == NoiseLevel.SUSPICIOUS
        assert result.verdict == Verdict.NO_MARK


# ══════════════════════════════════════════════════════════════════
#  ⑤ 三类噪声样本落入 ⚠️
# ══════════════════════════════════════════════════════════════════


class TestKnownNoiseSamples:
    """SOP 附录 A.4 / 13.5.2 实测噪声样本。"""

    def test_ifr_pin_noise(self, engine: JudgeEngine) -> None:
        """``IFR PIN``（= ``SKYWORTH P/N`` 误读）→ ⚠️。"""
        record = make_record(
            brand="SKYWORTH",
            model="SKYWORTH P/N",
            evidences=[make_evidence("IFR PIN")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_MARK
        assert result.verdict != Verdict.FAIL

    def test_boori_e339609_noise(self, engine: JudgeEngine) -> None:
        """``boori E339609``（= ``baori`` + 型号后缀）→ ⚠️（非 ❌）。"""
        record = make_record(
            brand="baori",
            model="E339609",
            evidences=[make_evidence("boori E339609")],
        )
        result = engine.judge(record)
        assert result.verdict != Verdict.FAIL
        assert result.verdict in (Verdict.NO_MARK, Verdict.PASS)

    def test_yutong_noise(self, engine: JudgeEngine) -> None:
        """``YUTONG``（= 宇同）→ 中文/拼音等价，落入 ⚠️（非 ❌）。"""
        record = make_record(
            brand="宇同",
            model="YT-100",
            evidences=[make_evidence("YUTONG")],
        )
        result = engine.judge(record)
        assert result.verdict != Verdict.FAIL, "YUTONG 是宇同的拼音，不得判 ❌"

    def test_skylworth_noise(self, engine: JudgeEngine) -> None:
        """**裁决 2（R4，T06-18）**：``SKYHORTH P/H`` 命中已知样本 → 纠正为
        ``SKYWORTH P/N`` → 与申报 ``SKYWORTH`` **不一致** → ❌。

        ⚠️ 原断言 ``== NO_MARK``（旧"已知误读独立判噪"行为）**已作废**（R4 裁决 2）。
        裁决 2 后依据 = ``SKYWORTH P/N`` ≠ ``SKYWORTH``（纠正后确定值不一致）。
        """
        record = make_record(
            brand="SKYWORTH",
            model="SKYWORTH P/N",
            evidences=[make_evidence("SKYHORTH P/H")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL, (
            f"裁决 2：SKYHORTH 纠正为 SKYWORTH P/N 后与申报 SKYWORTH 不一致，"
            f"应判 ❌，实际 {result.verdict}"
        )


# ══════════════════════════════════════════════════════════════════
#  ⑥ 外箱整机品牌（保留异常）
# ══════════════════════════════════════════════════════════════════


class TestWholeMachineBrand:
    """SOP 3.5 规则#8：外箱整机品牌 → 保留异常（不降级为合格）。"""

    def test_carton_daewoo_preserved_fail(self, engine: JudgeEngine) -> None:
        """``Brand:Daewoo`` + ``CARTON`` 上下文 → 保留 ❌（不降级）。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[
                make_evidence(
                    "Brand:Daewoo\nCARTON\nJOBNO:12345\nOrigin:CN",
                )
            ],
        )
        result = engine.judge(record)
        assert result.verdict != Verdict.PASS, "外箱整机品牌不得被判合格"
        assert result.verdict == Verdict.FAIL

    def test_whole_machine_note_present(self, engine: JudgeEngine) -> None:
        """命中整机品牌上下文时差异备注应含"整机品牌"说明。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("Brand:Daewoo\nCARTON\nJOBNO:888")],
        )
        result = engine.judge(record)
        notes = " ".join(d.note for d in result.differences)
        assert "整机品牌" in notes or "整机" in result.reason

    def test_guard_is_whole_machine_brand(self, rule_repo) -> None:
        """``NoiseGuard.is_whole_machine_brand`` 直接单测。"""
        from core.noise_guard import NoiseGuard

        guard = NoiseGuard(rule_repo)
        assert guard.is_whole_machine_brand("Brand:Daewoo\nCARTON\nJOBNO:1")
        assert guard.is_whole_machine_brand("品牌:PHILIPS\n唛头\n数量:10")
        assert not guard.is_whole_machine_brand("品牌:baori\n型号:A7A01G")

    def test_whole_machine_consistent_is_pass(self, engine: JudgeEngine) -> None:
        """整机品牌与申报值**一致**（仅大小写）时判合格，不得误报异常。

        口径澄清：SOP 3.5 规则#8「保留异常」是指"**确有异常时不得因整机品牌而洗白**"，
        而非"一见整机品牌就判异常"。取值一致时应当 ✅。
        """
        record = make_record(
            brand="DAEWOO",
            model="",
            evidences=[make_evidence("Brand:Daewoo\nCARTON\nJOBNO:12345")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS


# ══════════════════════════════════════════════════════════════════
#  NO_IMAGE 的 unreachable / not_found 区分（架构设计 13.13.4）
# ══════════════════════════════════════════════════════════════════


class TestImageGate:
    """图片闸门：两种缺图场景结论均 ``NO_IMAGE``，但内部信号不同。"""

    def test_not_found_no_image(self, engine: JudgeEngine) -> None:
        """目录不存在（``not_found``）→ ``NO_IMAGE``，正常继续。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[
                ImageEvidence(
                    image_path="/share/ticket/part",
                    seq=0,
                    exists=False,
                    not_found=True,
                    unreachable=False,
                )
            ],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_IMAGE
        assert "不存在" in result.reason or "缺图" in result.reason
        # ⚠️ 互斥红线（架构 13.13.4）：not_found **绝不**置 unreachable
        assert result.unreachable is False

    def test_unreachable_no_image(self, engine: JudgeEngine) -> None:
        """共享根不可达（``unreachable``）→ 仍 ``NO_IMAGE``，但标注不可达供 T05 暂停。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[
                ImageEvidence(
                    image_path="//172.20.99.220/share",
                    seq=0,
                    exists=False,
                    unreachable=True,
                    not_found=False,
                )
            ],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_IMAGE
        assert "不可达" in result.reason
        # 结构化暂停标志位（T05 唯一判据）
        assert result.unreachable is True

    def test_unreachable_flag_structured(self, engine: JudgeEngine) -> None:
        """``unreachable`` 是**结构化布尔位**（不靠 reason 文案匹配）。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[ImageEvidence(unreachable=True, not_found=False, exists=False)],
        )
        result = engine.judge(record)
        assert isinstance(result.unreachable, bool)
        assert result.unreachable is True
        assert result.verdict == Verdict.NO_IMAGE

    def test_not_found_unreachable_must_be_false(self, engine: JudgeEngine) -> None:
        """**硬断言**：``not_found=True`` 的记录 ``unreachable`` 必须为 ``False``。

        这是 T05 自动暂停的正确性前提 —— 用户票号填错时全部落到 ``not_found``，
        若误置 ``unreachable`` 会让 T05 连续 K 条挂起整批。
        """
        for i in range(3):
            record = make_record(
                brand="baori",
                model="A7A01G",
                evidences=[
                    ImageEvidence(
                        image_path=f"/share/missing/{i}",
                        seq=0,
                        exists=False,
                        not_found=True,
                        unreachable=False,
                    )
                ],
            )
            result = engine.judge(record)
            assert result.verdict == Verdict.NO_IMAGE
            assert result.unreachable is False, "not_found 场景不得置 unreachable"

    def test_unreachable_in_to_dict(self, engine: JudgeEngine) -> None:
        """``unreachable`` 进 ``to_dict()``（JSON 日志），但**不进 13 列**。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[ImageEvidence(unreachable=True, not_found=False, exists=False)],
        )
        result = engine.judge(record)
        payload = result.to_dict()
        assert payload["unreachable"] is True
        # 13 列结构不受影响（冻结）
        assert len(result.to_row()) == 13

    def test_mutual_exclusion(self) -> None:
        """``unreachable`` 与 ``not_found`` 互斥（硬约束）。"""
        for ev in [
            ImageEvidence(unreachable=True, not_found=False),
            ImageEvidence(unreachable=False, not_found=True),
        ]:
            assert not (ev.unreachable and ev.not_found)
            ev.to_dict()  # 可序列化


# ══════════════════════════════════════════════════════════════════
#  纯函数契约（无副作用 / 可重复调用）
# ══════════════════════════════════════════════════════════════════


class TestPureFunctionContract:
    """``JudgeEngine.judge`` 必须为纯函数。"""

    def test_repeated_calls_identical(self, engine: JudgeEngine) -> None:
        """同一输入重复调用结果一致。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:baori 型号:A7A01G")],
        )
        first = engine.judge(record)
        second = engine.judge(record)
        assert first.verdict == second.verdict
        assert first.detected_brand == second.detected_brand
        assert first.detected_model == second.detected_model
        assert first.to_dict() == second.to_dict()

    def test_no_side_effect_on_record(self, engine: JudgeEngine) -> None:
        """judge 不得修改入参 record。"""
        record = make_record(brand="baori", model="A7A01G", evidences=[make_evidence("品牌:baori")])
        before = record.to_dict()
        engine.judge(record)
        assert record.to_dict() == before

    def test_key_matches_record_key(self, engine: JudgeEngine) -> None:
        """``CheckResult.key`` 与 ``record.key()`` 一致。"""
        record = make_record(brand="baori", model="A7A01G", evidences=[make_evidence("品牌:baori")])
        result = engine.judge(record)
        assert result.key == record.key()

    def test_evidence_text_no_full_dump(self, engine: JudgeEngine) -> None:
        """证据片段有长度上限（不把完整原文灌进汇总表/日志）。"""
        long_text = "品牌:baori " + ("X" * 500)
        record = make_record(brand="baori", model="A7A01G", evidences=[make_evidence(long_text)])
        result = engine.judge(record)
        assert len(result.evidence_text) <= 200


# ══════════════════════════════════════════════════════════════════
#  OCR 提取辅助
# ══════════════════════════════════════════════════════════════════


class TestExtraction:
    """图片侧品牌 / 型号提取。"""

    def test_extract_brand(self) -> None:
        assert extract_detected_brand([make_ocr("品牌:baori")]) == "baori"
        assert extract_detected_brand([make_ocr("Brand:SAMSUNG")]) == "SAMSUNG"
        assert extract_detected_brand([make_ocr("数量201")]) == ""

    def test_extract_model(self) -> None:
        assert extract_detected_model([make_ocr("型号:A7A01G")]) == "A7A01G"
        assert extract_detected_model([make_ocr("Model:UA55")]) == "UA55"
        assert extract_detected_model([make_ocr("无关文本")]) == ""

    def test_ocr_from_evidences_used(self, engine: JudgeEngine) -> None:
        """未显式传 ocr_texts 时，从 ``record.evidences[*].ocr`` 取值。"""
        record = make_record(
            brand="baori",
            model="A7A01G",
            evidences=[make_evidence("品牌:baori 型号:A7A01G")],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert result.detected_brand == "baori"


# ══════════════════════════════════════════════════════════════════
#  T04 交付完整性守卫（文件存在 + 分层红线）
# ══════════════════════════════════════════════════════════════════


class TestT04DeliveryGuard:
    """T04 交付物完整性 + 分层红线（架构设计第 7 节 / 9.4）。"""

    def test_t04_core_modules_exist(self) -> None:
        """T04 五个 core 模块必须存在。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "core"
        for name in (
            "judge_engine.py",
            "noise_guard.py",
            "diff_util.py",
            "resume_store.py",
            "result_exporter.py",
        ):
            assert (root / name).is_file(), f"缺少 core/{name}"

    def test_t04_test_files_exist(self) -> None:
        """T04 四个测试文件必须存在。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent
        for name in (
            "test_judge_engine.py",
            "test_noise_guard.py",
            "test_exporter.py",
            "test_resume_store.py",
        ):
            assert (root / name).is_file(), f"缺少 tests/{name}"

    def test_judge_engine_does_not_import_element_parser(self) -> None:
        """判定引擎**禁止** import ``core.element_parser``（13.13.2 单向依赖）。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "core" / "judge_engine.py"
        ).read_text(encoding="utf-8")
        assert "import element_parser" not in src
        assert "from core.element_parser" not in src
