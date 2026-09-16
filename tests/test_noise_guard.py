"""T04 噪声护栏回归测试（``tests/test_noise_guard.py``）。

覆盖架构设计 12.R5 + 13.5.2：

  * 噪声三态（``DEFINITE_MATCH`` / ``SUSPICIOUS`` / ``CLEAR_MISMATCH``）判定
  * **不虚高红线**：``SUSPICIOUS`` 绝不直达 ``FAIL``（在 JudgeEngine 层有硬断言）
  * 易混字符归一化（并查集等价类，含 H 桥接 W/N）
  * 模糊相似度（编辑距离 ≤ 2 且长度 ≥ 5）
  * 低置信度 / 残片信号
  * 外箱整机品牌上下文
  * 字段名误读黑名单
  * 规则外置（注入 ``RuleRepository``）
"""

from __future__ import annotations

import pytest

from core.constants import (
    DEFAULT_CONFUSABLE_CHARS,
    DEFAULT_FUZZY_MAX_EDIT_DISTANCE,
    DEFAULT_FUZZY_MIN_LENGTH,
)
from core.models import NoiseLevel, OcrText
from core.noise_guard import NoiseGuard, edit_distance, is_none_token


def make_ocr(text: str, *, confidence: float = 0.95, low: bool = False) -> OcrText:
    """构造一条 OCR 结果。"""
    return OcrText(image_path="/img/1.jpg", text_raw=text, confidence=confidence, low_confidence=low)


@pytest.fixture
def guard(rule_repo) -> NoiseGuard:
    """注入规则仓库的噪声护栏。"""
    return NoiseGuard(rule_repo)


@pytest.fixture
def guard_no_rules() -> NoiseGuard:
    """无规则仓库的护栏（回退 constants 兜底默认值）。"""
    return NoiseGuard(None)


# ══════════════════════════════════════════════════════════════════
#  编辑距离
# ══════════════════════════════════════════════════════════════════


class TestEditDistance:
    """Levenshtein 编辑距离。"""

    def test_identical(self) -> None:
        assert edit_distance("abc", "abc") == 0

    def test_empty(self) -> None:
        assert edit_distance("", "abc") == 3
        assert edit_distance("abc", "") == 3

    def test_substitution(self) -> None:
        assert edit_distance("A7A01G", "A7A02G") == 1

    def test_skylworth_case(self) -> None:
        """实测锚点：``SKYWORTH P/N`` vs ``SKYHORTH P/H`` 距离 = 2。"""
        assert edit_distance("SKYWORTH P/N", "SKYHORTH P/H") == 2

    def test_case_insensitive(self) -> None:
        assert edit_distance("abc", "ABC") == 0


# ══════════════════════════════════════════════════════════════════
#  无值判定
# ══════════════════════════════════════════════════════════════════


class TestNoneToken:
    """``is_none_token`` 无值等价类。"""

    @pytest.mark.parametrize(
        "value",
        ["无", "无品牌", "空", "N/A", "NA", "NONE", "NULL", "未标记", "", "  "],
    )
    def test_none_tokens(self, value: str) -> None:
        assert is_none_token(value)

    @pytest.mark.parametrize("value", ["baori", "SAMSUNG", "宇同", "A7A01G"])
    def test_non_none_tokens(self, value: str) -> None:
        assert not is_none_token(value)


# ══════════════════════════════════════════════════════════════════
#  三态分类
# ══════════════════════════════════════════════════════════════════


class TestClassifyThreeStates:
    """三态分类核心用例。"""

    def test_definite_match_exact(self, guard: NoiseGuard) -> None:
        assert guard.classify("baori", "baori") == NoiseLevel.DEFINITE_MATCH

    def test_definite_match_both_absent(self, guard: NoiseGuard) -> None:
        assert guard.classify("无", "无") == NoiseLevel.DEFINITE_MATCH

    def test_definite_match_case_only_difference(self, guard: NoiseGuard) -> None:
        """仅大小写不同 → DEFINITE_MATCH（**不是噪声**）。

        申报口径常为全大写（``DAEWOO``），唛头印刷/OCR 常为混合大小写（``Daewoo``）。
        二者是**同一取值**，必须判 ✅；若落入"易混字符归一化"（内部会 ``.upper()``）
        会被误捕为 ⚠️，把本该合格的记录错误降级 —— 本用例锁死该回归。
        """
        assert guard.classify("DAEWOO", "Daewoo") == NoiseLevel.DEFINITE_MATCH
        assert guard.classify("baori", "Baori") == NoiseLevel.DEFINITE_MATCH
        assert guard.classify("SKYWORTH", "skyworth") == NoiseLevel.DEFINITE_MATCH

    def test_case_only_difference_detail_signal(self, guard: NoiseGuard) -> None:
        """大小写差异的 detail.signals 应标注 ``case_insensitive_equal``。"""
        detail = guard.classify_detail("DAEWOO", "Daewoo", field_name="品牌")
        assert detail.level == NoiseLevel.DEFINITE_MATCH
        assert "case_insensitive_equal" in detail.signals

    def test_suspicious_confusable(self, guard: NoiseGuard) -> None:
        """**裁决 2 变更（架构裁决文档 §4.6）**：``SKYHORTH P/H`` 命中已知噪声样本
        → **纠正**为 ``SKYWORTH P/N`` → 与申报 ``SKYWORTH P/N`` **一致** → ✅。

        原"命中已知样本 → SUSPICIOUS"的断言**已作废**（R4 裁决 2）；已知误读改为
        "自动纠正后比对"。纯易混字符（**未**在样本表中）仍应判 ``SUSPICIOUS``。
        """
        assert guard.classify("SKYWORTH P/N", "SKYHORTH P/H") == NoiseLevel.DEFINITE_MATCH

    def test_suspicious_confusable_only(self, guard: NoiseGuard) -> None:
        """纯易混字符（未命中已知样本表）→ ``SUSPICIOUS``（W/H、O/0 等，不虚高）。"""
        # ``ONEL`` vs ``ONE1``：O/0、I/1/l 易混，且不在 known_noise_samples 中
        assert guard.classify("SKYWORTH", "SKYWORTH") == NoiseLevel.DEFINITE_MATCH
        # 未在样本表中的易混：``ABC0`` vs ``ABCO``（0↔O）
        assert guard.classify("ABCO", "ABC0") == NoiseLevel.SUSPICIOUS

    def test_suspicious_fuzzy(self, guard: NoiseGuard) -> None:
        """编辑距离 ≤ 2 且长度 ≥ 5 → SUSPICIOUS。"""
        assert guard.classify("MODEL-12345", "MODEL-12390") == NoiseLevel.SUSPICIOUS

    def test_clear_mismatch(self, guard: NoiseGuard) -> None:
        """明显不同 → CLEAR_MISMATCH。"""
        assert guard.classify("SAMSUNG", "PHILIPS-LCD") == NoiseLevel.CLEAR_MISMATCH

    def test_clear_mismatch_short_different(self, guard: NoiseGuard) -> None:
        """短串且完全不同 → CLEAR_MISMATCH（长度不足不触发模糊）。"""
        assert guard.classify("AB", "XY") == NoiseLevel.CLEAR_MISMATCH

    def test_low_confidence_suspicious(self, guard: NoiseGuard) -> None:
        """低置信度 OCR → SUSPICIOUS。"""
        level = guard.classify(
            "SAMSUNG",
            "PHILIPS-TOTALLY-DIFFERENT",
            ocr_texts=[make_ocr("...", confidence=0.2, low=True)],
        )
        assert level == NoiseLevel.SUSPICIOUS

    def test_classify_detail_reason(self, guard: NoiseGuard) -> None:
        """``classify_detail`` 返回命中依据（裁决 2：命中样本 → 纠正后比对）。

        ``SKYHORTH P/H`` 命中样本 → 纠正为 ``SKYWORTH P/N`` → 与申报一致 → ✅，
        信号应含 ``known_misread_corrected``，reason 应记录纠正前后值（可审计）。
        """
        detail = guard.classify_detail("SKYWORTH P/N", "SKYHORTH P/H", field_name="型号")
        assert detail.level == NoiseLevel.DEFINITE_MATCH
        assert "known_misread_corrected" in detail.signals
        assert "纠正" in detail.reason
        assert detail.reason

    def test_classify_detail_uncorrected_confusable_reason(self, guard: NoiseGuard) -> None:
        """未命中样本表的纯易混 → ``SUSPICIOUS``，信号含 ``confusable_char``。"""
        detail = guard.classify_detail("ABCO", "ABC0", field_name="型号")
        assert detail.level == NoiseLevel.SUSPICIOUS
        assert "confusable_char" in detail.signals
        assert detail.reason


# ══════════════════════════════════════════════════════════════════
#  易混字符（并查集对称性）
# ══════════════════════════════════════════════════════════════════


class TestConfusableChars:
    """易混字符归一化（``NoiseSignalRules.normalize_confusables``）。"""

    def test_skylworth_normalization(self, guard: NoiseGuard) -> None:
        rules = guard.noise_rules
        assert rules.normalize_confusables("SKYWORTH") == rules.normalize_confusables("SKYHORTH")

    def test_pn_ph_normalization(self, guard: NoiseGuard) -> None:
        rules = guard.noise_rules
        assert rules.normalize_confusables("P/N") == rules.normalize_confusables("P/H")

    def test_symmetry(self, guard: NoiseGuard) -> None:
        """归一化是对称的（H 为桥接字符，W/N/H 同等价类）。"""
        rules = guard.noise_rules
        assert rules.normalize_confusables("W") == rules.normalize_confusables("H")
        assert rules.normalize_confusables("N") == rules.normalize_confusables("H")

    def test_unrelated_not_equal(self, guard: NoiseGuard) -> None:
        rules = guard.noise_rules
        assert rules.normalize_confusables("GXD-009") != rules.normalize_confusables("ABC-777")

    def test_confusable_groups_loaded(self, guard: NoiseGuard) -> None:
        """规则表加载了全部易混字符组。"""
        assert len(guard.noise_rules.confusable_chars) == len(DEFAULT_CONFUSABLE_CHARS)


# ══════════════════════════════════════════════════════════════════
#  模糊阈值（外置规则）
# ══════════════════════════════════════════════════════════════════


class TestFuzzyThresholds:
    """模糊相似度阈值来自外置规则。"""

    def test_threshold_values_from_rules(self, guard: NoiseGuard) -> None:
        assert guard.noise_rules.fuzzy_max_edit_distance == DEFAULT_FUZZY_MAX_EDIT_DISTANCE
        assert guard.noise_rules.fuzzy_min_length == DEFAULT_FUZZY_MIN_LENGTH

    def test_short_string_not_suspicious(self, guard: NoiseGuard) -> None:
        """长度 < min_length 不触发模糊（短串太易误判）。"""
        assert guard.classify("AB1", "AB2") == NoiseLevel.CLEAR_MISMATCH

    def test_verdict_is_suspicious(self, guard: NoiseGuard) -> None:
        """规则表的 fuzzy verdict 必须是 SUSPICIOUS（红线）。"""
        assert guard.noise_rules.fuzzy_verdict == "SUSPICIOUS"

    def test_no_rules_fallback(self, guard_no_rules: NoiseGuard) -> None:
        """无规则仓库时使用 constants 兜底默认值，行为一致。"""
        assert guard_no_rules.classify("SKYWORTH P/N", "SKYHORTH P/H") == NoiseLevel.SUSPICIOUS


# ══════════════════════════════════════════════════════════════════
#  外箱整机品牌
# ══════════════════════════════════════════════════════════════════


class TestWholeMachineBrand:
    """SOP 3.5 规则#8 外箱整机品牌上下文。"""

    def test_carton_context(self, guard: NoiseGuard) -> None:
        assert guard.is_whole_machine_brand("Brand:Daewoo\nCARTON\nJOBNO:1")

    def test_jobno_context(self, guard: NoiseGuard) -> None:
        assert guard.is_whole_machine_brand("Brand:Haier\nJOB NO: 1234\n数量:5")

    def test_maitou_context(self, guard: NoiseGuard) -> None:
        assert guard.is_whole_machine_brand("唛头\n品牌:PHILIPS\nMade in China")

    def test_no_context(self, guard: NoiseGuard) -> None:
        assert not guard.is_whole_machine_brand("品牌:baori\n型号:A7A01G\n数量:10")

    def test_empty_text(self, guard: NoiseGuard) -> None:
        assert not guard.is_whole_machine_brand("")


# ══════════════════════════════════════════════════════════════════
#  字段名误读黑名单
# ══════════════════════════════════════════════════════════════════


class TestFieldNameMisread:
    """SOP 3.5 规则#1/#3 字段名黑名单。"""

    @pytest.mark.parametrize(
        "value",
        ["制造商全称", "原产地", "MFR P/N", "Supplier Code", "创维物料编号", "制造商", "生产厂商"],
    )
    def test_blacklisted(self, guard: NoiseGuard, value: str) -> None:
        assert guard.is_field_name_misread(value)

    @pytest.mark.parametrize("value", ["baori", "SAMSUNG", "宇同"])
    def test_not_blacklisted(self, guard: NoiseGuard, value: str) -> None:
        assert not guard.is_field_name_misread(value)

    def test_empty(self, guard: NoiseGuard) -> None:
        assert not guard.is_field_name_misread("")


# ══════════════════════════════════════════════════════════════════
#  规则外置（注入仓库生效）
# ══════════════════════════════════════════════════════════════════


class TestRulesExternalized:
    """规则从仓库读取（不是硬编码）。"""

    def test_describe_rules(self, guard: NoiseGuard) -> None:
        summary = guard.describe_rules()
        assert summary["confusable_groups"] >= 8
        assert summary["fuzzy_max_edit_distance"] == DEFAULT_FUZZY_MAX_EDIT_DISTANCE
        assert summary["whole_machine_tokens"] >= 1

    def test_whole_machine_rules_loaded(self, guard: NoiseGuard) -> None:
        assert guard.whole_machine_rules.context_tokens
        assert guard.whole_machine_rules.context_window >= 1


# ══════════════════════════════════════════════════════════════════
#  缺陷 F 回归锁（架构裁决文档 §5B）：known_noise_samples 粒度对齐
# ══════════════════════════════════════════════════════════════════


class TestKnownNoiseSampleGranularity:
    """缺陷 F：运行期 P/N 行**前缀**形态必须能命中样本（粒度对齐）。

    根因：样本表存**整行** ``SKYHORTH P/H``，运行期 ``extract_detected_brand``
    ② 取 **P/N 行前缀** ``SKYHORTH`` → 旧实现既不"相等"也不"包含" → 永不命中。
    修法：样本表增 ``ocr_prefix`` + 匹配含"前缀子串兜底"。
    """

    def test_known_noise_sample_prefix_hit(self, guard: NoiseGuard) -> None:
        """缺陷 F 核心：运行期前缀形态必须命中（旧实现为 ``False``）。"""
        assert guard.is_known_noise_sample("SKYHORTH") is True
        assert guard.is_known_noise_sample("IFR") is True
        assert guard.is_known_noise_sample("boori") is True
        assert guard.is_known_noise_sample("600-CX9") is True

    def test_whole_line_form_still_hits(self, guard: NoiseGuard) -> None:
        """整行形态仍命中（向后兼容）。"""
        assert guard.is_known_noise_sample("SKYHORTH P/H") is True
        assert guard.is_known_noise_sample("IFR PIN") is True
        assert guard.is_known_noise_sample("boori E339609") is True
        assert guard.is_known_noise_sample("600-CX9") is True

    def test_unknown_value_not_hit(self, guard: NoiseGuard) -> None:
        """未收录的值不得命中（防误伤）。"""
        assert guard.is_known_noise_sample("DAEWOO") is False
        assert guard.is_known_noise_sample("") is False

    def test_correct_known_misread(self, guard: NoiseGuard) -> None:
        """裁决 2（§4.6）：``correct_known_misread`` 返回 ``expected_actual``；未命中返原值。"""
        assert guard.correct_known_misread("SKYHORTH") == "SKYWORTH P/N"
        assert guard.correct_known_misread("IFR") == "SKYWORTH P/N"
        assert guard.correct_known_misread("boori") == "baori"
        assert guard.correct_known_misread("600-CX9") == "GXD-009"
        assert guard.correct_known_misread("DAEWOO") == "DAEWOO"

    def test_known_misread_corrected_then_compared(self, guard: NoiseGuard) -> None:
        """裁决 2（§4.6）：``SKYHORTH``→纠正 ``SKYWORTH P/N``→与 ``DAEWOO``
        比对 → **❌**（**不再是 ⚠️**）。"""
        verdict = guard.classify_detail("DAEWOO", "SKYHORTH", field_name="品牌")
        assert verdict.level == NoiseLevel.CLEAR_MISMATCH
        assert "known_misread_corrected" in verdict.signals
        assert "纠正" in verdict.reason

    def test_known_misread_corrected_then_match(self, guard: NoiseGuard) -> None:
        """裁决 2（§4.6）：纠正后与申报**一致** → ``DEFINITE_MATCH``（✅）。"""
        verdict = guard.classify_detail("SKYWORTH P/N", "SKYHORTH", field_name="品牌")
        assert verdict.level == NoiseLevel.DEFINITE_MATCH
        assert "known_misread_corrected" in verdict.signals
