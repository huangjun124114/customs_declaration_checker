"""英文「只差字母」→ 待复核 回归测试（口径 v0.3.2，用户裁定 2026-09-17）。

    需求 3：申报值与 OCR 识别值**都是英文**、英文长度**超过 3 个字母**、
            两者**只有字母之差** → 列为**待复核（⚠️）**，并写明复核说明。

⚠️ 本文件锁三件事：
  ① **命中即降级**：纯英文、长度均 ≥ 4、编辑距离 ≤ 2 → ``SUSPICIOUS``（⚠️，绝不 ❌）；
  ② **边界**：长度不足 4 / 含数字符号 / 编辑距离超限 → 维持 ``CLEAR_MISMATCH``（可 ❌）；
     中文不得被 ``str.isalpha()`` 误纳入（必须显式判 ASCII）；
  ③ **可追溯**：端到端判 ⚠️ 时，「判定依据」须含**逐字符差异**，判定说明须写明
     "仅字母之差 / 疑似 OCR 字母误读"。
"""

from __future__ import annotations

import pytest

from core.constants import (
    DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE,
    DEFAULT_LETTER_DIFF_MIN_LENGTH,
    FIELD_BRAND,
)
from core.judge_engine import JudgeEngine
from core.models import NoiseLevel, Verdict
from core.noise_guard import NoiseGuard
from tests.test_judge_engine import make_evidence, make_record

# ══════════════════════════════════════════════════════════════════
#  ① 护栏级别（NoiseGuard）
# ══════════════════════════════════════════════════════════════════


class TestLetterOnlyDifference:
    """纯英文「只差字母」→ SUSPICIOUS。"""

    @pytest.fixture
    def guard(self, rule_repo) -> NoiseGuard:
        return NoiseGuard(rule_repo)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # 长度恰为 4：通用模糊相似度（要求长度 ≥ 5）**不覆盖** → 必须由新规则接住
            ("ABCD", "AXCD"),
            ("ABCD", "ABXD"),
            # 长度 ≥ 5：既有 fuzzy_similarity 已覆盖（同样判 ⚠️）；此处锁"殊途同归"
            ("BOORI", "BAORI"),
            ("SKYWORTH", "SKYWOOTH"),
            ("ABCDEFG", "ABCDXFG"),
        ],
    )
    def test_letter_only_diff_is_suspicious(
        self, guard: NoiseGuard, left: str, right: str
    ) -> None:
        verdict = guard.classify_detail(left, right, field_name="品牌")
        assert verdict.level == NoiseLevel.SUSPICIOUS
        assert "人工复核" in verdict.reason

    @pytest.mark.parametrize(("left", "right"), [("ABCD", "AXCD"), ("ABCD", "ABXD")])
    def test_new_rule_is_the_one_catching_len4(
        self, guard: NoiseGuard, left: str, right: str
    ) -> None:
        """长度 4 的场景必须由 ``letter_only_difference`` 接住（旧链路会判 ❌）。"""
        verdict = guard.classify_detail(left, right, field_name="品牌")
        assert "letter_only_difference" in verdict.signals

    def test_signal_is_never_clear_mismatch(self, guard: NoiseGuard) -> None:
        """不虚高红线：命中该规则**绝不**返回 CLEAR_MISMATCH。"""
        assert guard.classify("ABCD", "AXCD") == NoiseLevel.SUSPICIOUS
        assert guard.classify("ABCD", "AXCD") != NoiseLevel.CLEAR_MISMATCH


class TestLetterDiffBoundaries:
    """边界：不该命中的必须不命中。"""

    @pytest.fixture
    def guard(self, rule_repo) -> NoiseGuard:
        return NoiseGuard(rule_repo)

    def test_too_short_not_hit(self, guard: NoiseGuard) -> None:
        """长度 3（未"超过 3 个字母"）→ 不命中该规则。"""
        assert not guard._is_letter_only_difference("ABC", "AXC")  # noqa: SLF001
        assert guard.classify_detail("ABC", "AXC").level == NoiseLevel.CLEAR_MISMATCH

    def test_digits_excluded(self, guard: NoiseGuard) -> None:
        """含数字 → 不命中该规则（数字之差是实体差异，非字母误读）。

        ⚠️ 注意：``A7A01G``/``A7A02G`` 属**长度 ≥ 5**，会被既有 ``fuzzy_similarity``
        判 ⚠️ —— 那是 v0.1.0 起的既有口径，与本轮新规则无关（故用私有规则函数断言边界）。
        """
        assert not guard._is_letter_only_difference("AB12", "AB13")  # noqa: SLF001
        assert not guard._is_letter_only_difference("A7A01G", "A7A02G")  # noqa: SLF001
        # 长度 4 时既无 fuzzy 也无新规则 → 维持"明确不一致"（可 ❌）
        assert guard.classify_detail("AB12", "AB13").level == NoiseLevel.CLEAR_MISMATCH

    def test_symbols_excluded(self, guard: NoiseGuard) -> None:
        assert not guard._is_letter_only_difference("AB-CD", "AB-CE")  # noqa: SLF001
        assert guard.classify_detail("AB-C", "AB-D").level == NoiseLevel.CLEAR_MISMATCH

    def test_distance_over_limit_not_hit(self, guard: NoiseGuard) -> None:
        """编辑距离 3 > 上限 2 → 明确不一致（可 ❌）。"""
        assert not guard._is_letter_only_difference("ABCDEF", "AXYZEF")  # noqa: SLF001
        assert guard.classify_detail("ABCDEF", "AXYZEF").level == NoiseLevel.CLEAR_MISMATCH

    def test_unrelated_brands_still_fail(self, guard: NoiseGuard) -> None:
        """完全不同的品牌仍判明确不一致（不能被"都是英文"放过）。"""
        assert not guard._is_letter_only_difference("DAEWOO", "SKYWORTH")  # noqa: SLF001
        assert guard.classify_detail("DAEWOO", "SKYWORTH").level == NoiseLevel.CLEAR_MISMATCH

    def test_chinese_excluded(self, guard: NoiseGuard) -> None:
        """⚠️ 中文**不得**被纳入：``str.isalpha()`` 对中文同样为 True，必须显式判 ASCII。"""
        assert not guard._is_letter_only_difference("宇同电子", "宇同电器")  # noqa: SLF001
        assert guard.classify_detail("宇同电子", "宇同电器").level == NoiseLevel.CLEAR_MISMATCH

    def test_case_only_difference_is_match(self, guard: NoiseGuard) -> None:
        """仅大小写不同 → 明确匹配（不是噪声）。"""
        verdict = guard.classify_detail("ABCD", "abcd")
        assert verdict.level == NoiseLevel.DEFINITE_MATCH
        assert not verdict.signals.count("letter_only_difference")

    def test_defaults_from_constants(self) -> None:
        assert DEFAULT_LETTER_DIFF_MIN_LENGTH == 4          # 长度 > 3
        assert DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE == 2


class TestRulesSource:
    """规则外置：YAML → 快照 → 生效。"""

    def test_yaml_values_used(self, rule_repo) -> None:
        rules = rule_repo.get().noise_signals
        assert rules.letter_diff_min_length == DEFAULT_LETTER_DIFF_MIN_LENGTH
        assert rules.letter_diff_max_edit_distance == DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE
        assert rules.letter_diff_verdict == "SUSPICIOUS"

    def test_guard_without_repo_still_hits(self) -> None:
        """兜底路径（无 YAML）也必须命中 —— 兜底 ≡ repo（缺陷 F 教训）。"""
        guard = NoiseGuard(None)
        assert guard.classify_detail("ABCD", "AXCD").level == NoiseLevel.SUSPICIOUS


# ══════════════════════════════════════════════════════════════════
#  ② 端到端（JudgeEngine）：待复核 + 差异说明
# ══════════════════════════════════════════════════════════════════


class TestJudgeLetterDiffEndToEnd:
    """申报/识别只有字母之差 → ⚠️ 待人工复核（不是 ❌）。"""

    @pytest.fixture
    def engine(self, rule_repo) -> JudgeEngine:
        return JudgeEngine(rules=rule_repo)

    def test_brand_letter_diff_goes_to_review(self, engine: JudgeEngine) -> None:
        record = make_record(brand="ABCD", model="A7A01G", evidences=[
            make_evidence("品牌:AXCD\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_MARK, result.reason
        assert result.verdict != Verdict.FAIL
        assert "字母" in result.reason
        # 逐字符差异须落到「判定依据」列（SOP 3.4 规则 5 + 便于人工复核）
        brand_diff = next(d for d in result.differences if d.field == FIELD_BRAND)
        assert brand_diff.char_diffs
        assert "B" in "".join(brand_diff.char_diffs)
        assert "X" in "".join(brand_diff.char_diffs)
        # 列数冻结
        assert len(result.to_row()) == 13

    def test_model_letter_diff_goes_to_review(self, engine: JudgeEngine) -> None:
        record = make_record(brand="baori", model="ABCD", evidences=[
            make_evidence("baori\n型号:AXCD", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.NO_MARK

    def test_unrelated_brand_still_fail(self, engine: JudgeEngine) -> None:
        """对照：完全不同的品牌仍是 ❌（新规则没有放宽真异常）。

        ⚠️ 图内用 ``HAIER`` 而非 ``SKYWORTH``：后者命中 ``fields_blacklist``
        （``SKYWORTH P/N`` = 创维物料编号的英文字段名）→ 既有链路会把它剔成"无标识"
        → 判 ⚠️。那是缺陷 C 的既有口径，不在本轮范围内。
        """
        record = make_record(brand="DAEWOO", model="A7A01G", evidences=[
            make_evidence("品牌:HAIER\n型号:A7A01G", seq=1),
        ])
        assert engine.judge(record).verdict == Verdict.FAIL

    def test_suspicious_never_fail(self, engine: JudgeEngine) -> None:
        """不虚高红线的端到端锁。"""
        record = make_record(brand="ABCD", model="A7A01G", evidences=[
            make_evidence("品牌:AXCD\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert not (result.noise_level == NoiseLevel.SUSPICIOUS and result.verdict == Verdict.FAIL)
