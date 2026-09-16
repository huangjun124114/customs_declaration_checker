"""``core.kv_extractor`` 回归测试（v0.2.0 批次 2 / 点 8 的 KV 部分）。

覆盖：
  1. 各类分隔形态（半角/全角冒号、``=``、连续空格弱分隔）；
  2. 值清洗（命中字段名黑名单 / 非品牌裸 token → 置空）；
  3. 噪声行 / 非 KV 行进 ``residue``；
  4. 「值在下一行」开关；
  5. ``extract_from_ocr`` 便捷入口；
  6. **规则等价回归锁**（默认构造 ≡ repo 加载；防打包漏 YAML 静默失效）；
  7. 与 ``constants`` 默认值一致性（防两处漂移）。
"""

from __future__ import annotations

import dataclasses

import pytest

from core import constants as C
from core.kv_extractor import (
    KvExtractor,
    OcrLine,
    default_kv_rules,
    extract_kv,
)
from core.models import OcrText
from core.rule_repository import OcrKvPatternRules, RuleRepository

# ══════════════════════════════════════════════════════════════════
#  一、各类分隔形态
# ══════════════════════════════════════════════════════════════════


class TestSeparatorForms:
    """半角/全角冒号、等号、连续空格弱分隔。"""

    def test_halfwidth_colon(self) -> None:
        kv, _ = extract_kv(["Brand:Skyworth"])
        assert kv == {"Brand": "Skyworth"}

    def test_fullwidth_colon(self) -> None:
        kv, _ = extract_kv(["品牌：创维"])
        assert kv == {"品牌": "创维"}

    def test_model_upper(self) -> None:
        kv, _ = extract_kv(["MODEL:HS-8AA"])
        assert kv == {"MODEL": "HS-8AA"}

    def test_mfr_pn_with_space(self) -> None:
        kv, _ = extract_kv(["MFR P/N: xxx"])
        assert kv == {"MFR P/N": "xxx"}

    def test_equals_sign(self) -> None:
        kv, _ = extract_kv(["Brand=Skyworth"])
        assert kv == {"Brand": "Skyworth"}

    def test_whitespace_weak_separator_hits_alias(self) -> None:
        kv, _ = extract_kv(["Brand  Skyworth"])
        assert kv == {"Brand": "Skyworth"}

    def test_whitespace_single_space_is_not_separator(self) -> None:
        """单个空格不构成弱分隔（避免把普通文本切成 KV）。"""
        kv, residue = extract_kv(["hello world"])
        assert kv == {}
        assert residue == ["hello world"]

    def test_whitespace_key_must_hit_alias(self) -> None:
        """弱分隔键须命中别名白名单（``批次号`` 非别名 → 不采信）。"""
        kv, residue = extract_kv(["批次号  12345"])
        assert kv == {}
        assert residue == ["批次号  12345"]

    def test_unknown_key_with_explicit_separator_still_captured(self) -> None:
        """显式分隔符下，字段样态的未知键仍可捕获（归档展示用）。"""
        kv, _ = extract_kv(["批次号:12345"])
        assert kv == {"批次号": "12345"}

    def test_first_occurrence_wins(self) -> None:
        """同一键重复出现：首个非空值优先。"""
        kv, _ = extract_kv(["Brand:A", "Brand:B"])
        assert kv == {"Brand": "A"}

    def test_empty_value_filled_by_later_line(self) -> None:
        """首现值为空时，后续非空值补齐。"""
        kv, _ = extract_kv(["Brand:", "Brand:Skyworth"])
        assert kv == {"Brand": "Skyworth"}


# ══════════════════════════════════════════════════════════════════
#  二、值清洗（黑名单 / 非品牌裸 token）
# ══════════════════════════════════════════════════════════════════


class TestValueCleaning:
    """值命中字段名黑名单 / 非品牌裸 token → 置空（历史缺陷 C 教训）。"""

    def test_value_field_name_fragment_becomes_empty(self) -> None:
        """``Brand: SKYWORTH P/N`` → 值实为字段名残片 → 置空。"""
        kv, _ = extract_kv(["Brand: SKYWORTH P/N"])
        assert kv == {"Brand": ""}

    def test_value_non_brand_token_becomes_empty(self) -> None:
        """``Brand: prime`` → 命中非品牌裸 token → 置空。"""
        kv, _ = extract_kv(["Brand: prime"])
        assert kv == {"Brand": ""}

    def test_normal_value_kept(self) -> None:
        kv, _ = extract_kv(["Brand: 创维"])
        assert kv == {"Brand": "创维"}


# ══════════════════════════════════════════════════════════════════
#  三、噪声 / residue
# ══════════════════════════════════════════════════════════════════


class TestResidue:
    """非 KV 行与噪声行进 ``residue``。"""

    def test_free_text_to_residue(self) -> None:
        kv, residue = extract_kv(["品牌:创维", "这是一段自由文本 123", "型号:HS-8AA"])
        assert set(kv) == {"品牌", "型号"}
        assert residue == ["这是一段自由文本 123"]

    def test_noise_line_to_residue(self) -> None:
        kv, residue = extract_kv(["prime video"])
        assert kv == {}
        assert residue == ["prime video"]

    def test_blank_lines_skipped(self) -> None:
        kv, residue = extract_kv(["", "   ", "Brand:X"])
        assert kv == {"Brand": "X"}
        assert residue == []

    def test_all_lines_are_kv(self) -> None:
        kv, residue = extract_kv(["Brand:A", "MODEL:B", "QTY:1"])
        assert set(kv) == {"Brand", "MODEL", "QTY"}
        assert residue == []


# ══════════════════════════════════════════════════════════════════
#  四、「值在下一行」开关
# ══════════════════════════════════════════════════════════════════


class TestNextLineValue:
    """默认关闭；显式开启后支持「键在上一行、值在下一行」。"""

    def test_disabled_by_default(self) -> None:
        kv, residue = extract_kv(["品牌", "创维"])
        assert kv == {}
        assert residue == ["品牌", "创维"]

    def test_enabled_captures_next_line(self) -> None:
        rules = dataclasses.replace(default_kv_rules(), allow_next_line_value=True)
        kv, _ = KvExtractor(kv_rules=rules).extract(["品牌", "创维"])
        assert kv == {"品牌": "创维"}

    def test_enabled_does_not_consume_kv_line(self) -> None:
        rules = dataclasses.replace(default_kv_rules(), allow_next_line_value=True)
        kv, _ = KvExtractor(kv_rules=rules).extract(["品牌", "MODEL:X"])
        assert "品牌" not in kv
        assert kv.get("MODEL") == "X"


# ══════════════════════════════════════════════════════════════════
#  五、extract_from_ocr + 输入形态
# ══════════════════════════════════════════════════════════════════


class TestExtractFromOcr:
    """直接把 ``OcrText`` 解析为 KV。"""

    def test_from_ocr_text(self) -> None:
        ocr = OcrText(text_raw="Brand:Skyworth\n型号:HS-8AA", confidence=0.9)
        kv, _ = KvExtractor().extract_from_ocr(ocr)
        assert kv["Brand"] == "Skyworth"
        assert kv["型号"] == "HS-8AA"

    def test_accepts_str_and_ocrline_and_tuple(self) -> None:
        extractor = KvExtractor()
        kv, _ = extractor.extract(
            ["Brand:A", OcrLine(text="MODEL:B", score=0.9), ("QTY:C", 0.8, (0, 0, 1, 1))]
        )
        assert kv == {"Brand": "A", "MODEL": "B", "QTY": "C"}

    def test_accepts_objects_with_text_attr(self) -> None:
        class _Line:
            def __init__(self, text: str) -> None:
                self.text = text
                self.score = 0.9
                self.box = (0, 0, 1, 1)

        kv, _ = KvExtractor().extract([_Line("Brand:Z")])
        assert kv == {"Brand": "Z"}


# ══════════════════════════════════════════════════════════════════
#  六、规则等价回归锁（⚠️ 防打包漏 YAML 静默失效）
# ══════════════════════════════════════════════════════════════════


class TestRuleEquivalence:
    """默认构造的规则集 ≡ 从 repo 加载的规则集（历史缺陷 F 教训）。"""

    def test_kv_rules_default_and_repo_agree(self, rule_repo: RuleRepository) -> None:
        default = default_kv_rules()
        loaded = rule_repo.get().ocr_kv_patterns
        assert default.field_aliases == loaded.field_aliases
        assert default.separators == loaded.separators
        assert default.whitespace_separator_min == loaded.whitespace_separator_min
        assert default.noise_terms == loaded.noise_terms
        assert default.allow_next_line_value == loaded.allow_next_line_value

    @pytest.mark.parametrize(
        "lines",
        [
            ["Brand:Skyworth"],
            ["品牌：创维"],
            ["MODEL:HS-8AA"],
            ["MFR P/N: xxx"],
            ["Brand  Skyworth"],
            ["random free text"],
            ["prime video"],
            ["Brand: SKYWORTH P/N"],
            ["品牌", "创维"],
        ],
    )
    def test_default_and_repo_extractor_behave_identically(
        self, rule_repo: RuleRepository, lines: list[str]
    ) -> None:
        default_out = KvExtractor().extract(list(lines))
        repo_out = KvExtractor(rule_repo).extract(list(lines))
        assert default_out == repo_out, lines


# ══════════════════════════════════════════════════════════════════
#  七、与 constants 一致性（防两处漂移）
# ══════════════════════════════════════════════════════════════════


class TestConsistencyWithConstants:
    """``ocr_kv_patterns.yaml`` 与 ``constants`` 默认值一致。"""

    def test_field_aliases_match(self, rule_repo: RuleRepository) -> None:
        loaded = [
            [name, list(aliases)] for name, aliases in rule_repo.get().ocr_kv_patterns.field_aliases
        ]
        expected = [[name, list(aliases)] for name, aliases in C.DEFAULT_KV_FIELD_ALIASES]
        assert loaded == expected

    def test_separators_match(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().ocr_kv_patterns.separators) == C.DEFAULT_KV_SEPARATORS

    def test_whitespace_min_match(self, rule_repo: RuleRepository) -> None:
        assert (
            rule_repo.get().ocr_kv_patterns.whitespace_separator_min
            == C.DEFAULT_KV_WHITESPACE_SEPARATOR_MIN
        )

    def test_noise_terms_match(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().ocr_kv_patterns.noise_terms) == C.DEFAULT_KV_NOISE_TERMS

    def test_allow_next_line_match(self, rule_repo: RuleRepository) -> None:
        assert (
            rule_repo.get().ocr_kv_patterns.allow_next_line_value
            == C.DEFAULT_KV_ALLOW_NEXT_LINE_VALUE
        )

    def test_default_kv_rules_built_from_constants(self) -> None:
        rules = default_kv_rules()
        assert isinstance(rules, OcrKvPatternRules)
        assert rules.field_aliases == tuple(
            (name, tuple(aliases)) for name, aliases in C.DEFAULT_KV_FIELD_ALIASES
        )
        assert rules.separators == tuple(C.DEFAULT_KV_SEPARATORS)
        assert rules.noise_terms == tuple(C.DEFAULT_KV_NOISE_TERMS)
