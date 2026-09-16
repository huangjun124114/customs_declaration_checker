"""``core.token_matcher`` 单元测试（v0.3.0 需求 1）。

覆盖三层能力：

  * **EXACT**：英文/数字词边界、中文子串、大小写无关、多图命中；
  * **字段名护栏**：中文紧邻后缀 / 英文词边界后缀 / 词表整串命中 → 命中作废；
    以及**护栏不得误伤**（值后接空格 + 别字段、标签在前、裸值行）；
  * **FUZZY**：已知误读样本纠正、易混字符等价类（``JYB`` ↔ ``JY8``）。
"""

from __future__ import annotations

import pytest

from core.models import OcrText
from core.token_matcher import (
    MATCH_EXACT,
    MATCH_FUZZY,
    MATCH_NONE,
    TokenMatch,
    TokenMatcher,
)


def make_ocr(text: str, seq: int = 1, path: str = "") -> OcrText:
    """构造一张图的 OCR 结果。"""
    return OcrText(
        image_path=path or f"D:/imgs/A&{seq:03d}.jpg",
        text_raw=text,
        seq=seq,
        confidence=0.95,
    )


def matcher(*texts: str, rules=None, seqs: list[int] | None = None) -> TokenMatcher:
    """按文本列表构造匹配器（默认序号 1..n）。"""
    order = seqs or list(range(1, len(texts) + 1))
    return TokenMatcher(
        [make_ocr(t, seq=s) for t, s in zip(texts, order, strict=False)],
        rules=rules,
    )


# ══════════════════════════════════════════════════════════════════
#  ① EXACT —— 完整分词精确命中
# ══════════════════════════════════════════════════════════════════
class TestExactAsciiBoundary:
    """英文/数字按**词边界**（用户口径：JYB 命中 `品牌:JYB`，不命中 `JYBA01`）。"""

    @pytest.mark.parametrize(
        "line",
        ["品牌:JYB", "Brand:JYB", "JYB", "JYB-100", "型号 JYB A7", "（JYB）"],
    )
    def test_hits_complete_token(self, line: str) -> None:
        result = matcher(line).match("JYB", field="品牌")
        assert result.mode == MATCH_EXACT, f"{line!r} 应命中"
        assert result.images == [1]

    @pytest.mark.parametrize("line", ["JYBA01", "AJYB", "JYB9", "_JYB", "JYB_A01"])
    def test_misses_partial_token(self, line: str) -> None:
        """``JYB`` 不是完整分词（前后紧邻字母数字）→ **不命中**（防虚高）。"""
        assert matcher(line).match("JYB", field="品牌").mode == MATCH_NONE

    def test_case_insensitive(self) -> None:
        """唛头大小写与申报口径不同 → 仍算完整分词命中。"""
        result = matcher("Brand:Daewoo").match("DAEWOO", field="品牌")
        assert result.mode == MATCH_EXACT
        assert result.token == "Daewoo"

    def test_token_keeps_image_form(self) -> None:
        """``token`` 保留图内实际形态（供 UI 展示与追溯）。"""
        assert matcher("baori").match("baori", field="品牌").token == "baori"

    def test_multiple_images_recorded(self) -> None:
        """跨图命中 → ``images`` 去重升序（供 UI「命中来源图号」）。"""
        result = matcher("nope", "品牌:JYB", "JYBA01", "JYB-100", seqs=[1, 3, 5, 7]).match(
            "JYB", field="品牌"
        )
        assert result.mode == MATCH_EXACT
        assert result.images == [3, 7]

    def test_sample_line_truncated(self) -> None:
        """命中行超长时截断（≤121 字符，含省略号）。"""
        long_line = "品牌:JYB " + "X" * 400
        result = matcher(long_line).match("JYB", field="品牌")
        assert result.mode == MATCH_EXACT
        assert len(result.sample_line) <= 121


class TestExactChineseSubstring:
    """中文按**子串**（``宇同`` 可命中 ``宇同电子有限公司``）。"""

    def test_hits_inside_longer_cjk_phrase(self) -> None:
        assert matcher("宇同电子有限公司").match("宇同", field="品牌").mode == MATCH_EXACT

    def test_hits_label_form(self) -> None:
        assert matcher("品牌：宇同").match("宇同", field="品牌").mode == MATCH_EXACT

    def test_misses_when_absent(self) -> None:
        assert matcher("东莞宝瑞电子").match("宇同", field="品牌").mode == MATCH_NONE


# ══════════════════════════════════════════════════════════════════
#  ② 字段名护栏（防新链路引入虚高）
# ══════════════════════════════════════════════════════════════════
class TestFieldNameGuard:
    """中文子串匹配会把**字段名**当值 → 必须作废（方案 §3.2 / 风险 R1）。"""

    def test_cjk_adjacent_suffix_voided(self, rule_repo) -> None:
        """``创维`` 命中 ``创维物料编号``（字段名）→ **作废**（否则误判 ✅）。"""
        result = matcher("创维物料编号", rules=rule_repo).match("创维", field="品牌")
        assert result.mode == MATCH_NONE
        assert "字段名语境" in result.note

    def test_cjk_suffix_variants_voided(self, rule_repo) -> None:
        for line in ("创维编号", "创维代码", "创维名称", "创维全称", "创维供应商"):
            result = matcher(line, rules=rule_repo).match("创维", field="品牌")
            assert result.mode == MATCH_NONE, f"{line} 属字段名语境，应作废"

    def test_ascii_suffix_voided(self, rule_repo) -> None:
        """``SKYWORTH`` 命中 ``SKYWORTH P/N``（= 创维物料编号字段名）→ 作废。"""
        assert matcher("SKYWORTH P/N", rules=rule_repo).match("SKYWORTH", field="品牌").mode == (
            MATCH_NONE
        )

    def test_ascii_multiword_suffix_voided(self, rule_repo) -> None:
        """``Manufacturer`` 命中 ``Manufacturer Name`` → 作废（唛头字段名误判）。"""
        assert matcher(
            "Manufacturer Name", rules=rule_repo
        ).match("Manufacturer", field="品牌").mode == MATCH_NONE

    def test_blacklist_term_line_voided(self, rule_repo) -> None:
        """整行归一化后等于字段名词条 → 作废。"""
        assert matcher("MFR P/N", rules=rule_repo).match("MFR", field="品牌").mode == MATCH_NONE

    def test_voided_note_is_traceable(self, rule_repo) -> None:
        """命中被护栏作废时，note 必须说明原因（**不静默丢弃**，可追溯红线）。"""
        result = matcher("创维物料编号", rules=rule_repo).match("创维", field="品牌")
        assert "不构成值证据" in result.note
        assert result.images == []

    def test_guard_does_not_fire_on_plain_value(self, rule_repo) -> None:
        """裸值行 / 标签在前 / 后接别字段的值 —— 护栏**不得误伤**。"""
        cases = [
            ("baori", "baori"),
            ("Brand:Daewoo", "Daewoo"),
            ("品牌:创维", "创维"),
            ("品牌:创维 数量:10", "创维"),
            ("宇同电子有限公司", "宇同"),
            ("N011901-007386-001 baori", "baori"),
        ]
        for line, value in cases:
            result = matcher(line, rules=rule_repo).match(value, field="品牌")
            assert result.mode == MATCH_EXACT, f"{line!r} 中的 {value!r} 不应被护栏作废"

    def test_guard_prefers_valid_hit_over_voided_one(self, rule_repo) -> None:
        """同一值既有字段名命中又有正常命中 → 取**正常**命中。"""
        result = matcher("创维物料编号\n品牌:创维", rules=rule_repo).match("创维", field="品牌")
        assert result.mode == MATCH_EXACT
        assert result.sample_line == "品牌:创维"

    def test_guard_window_ignores_no_inside_part_number(self, rule_repo) -> None:
        """``NO`` 后缀须词边界：``N011901`` 内的 ``NO`` 不得触发护栏。"""
        result = matcher("品牌:JYB N011901", rules=rule_repo).match("JYB", field="品牌")
        assert result.mode == MATCH_EXACT


# ══════════════════════════════════════════════════════════════════
#  ③ FUZZY —— OCR 误读容错
# ══════════════════════════════════════════════════════════════════
class TestFuzzy:
    """容错保留（用户拍板：不丢召回），但必须标注「疑似误读，已纠正」。"""

    def test_confusable_hit_records_misread(self, rule_repo) -> None:
        """``JYB`` ↔ ``JY8``（8/B 易混）→ FUZZY，记录图内误读形态。"""
        result = matcher("品牌:JY8", rules=rule_repo).match("JYB", field="品牌")
        assert result.mode == MATCH_FUZZY
        assert result.corrected_from == "JY8"
        assert result.token == "JYB"

    def test_known_sample_corrected(self, rule_repo) -> None:
        """``baori`` 命中已知误读 ``boori`` → 按 ``expected_actual`` 纠正后命中。"""
        result = matcher("boori E339609", rules=rule_repo).match("baori", field="品牌")
        assert result.mode == MATCH_FUZZY
        assert result.corrected_from == "boori E339609"

    def test_known_sample_target_is_field_name_skipped(self, rule_repo) -> None:
        """**同源护栏**：``SKYHORTH P/H`` 纠正目标 ``SKYWORTH P/N`` **本身是字段名**
        → 不得据此判命中（否则申报品牌 ``SKYWORTH`` 仅凭字段名就"合格"）。"""
        assert matcher("SKYHORTH P/H", rules=rule_repo).match("SKYWORTH", field="品牌").mode == (
            MATCH_NONE
        )

    def test_known_sample_target_is_value_kept(self, rule_repo) -> None:
        """纠正目标是**真值**（``GXD-009``）时，容错仍然生效（不丢召回）。"""
        result = matcher("600-CX9", rules=rule_repo).match("GXD-009", field="型号")
        assert result.mode == MATCH_FUZZY
        assert result.token == "GXD-009"

    def test_confusable_not_triggered_when_exact_exists(self, rule_repo) -> None:
        """精确命中优先于模糊命中（EXACT 覆盖 FUZZY）。"""
        result = matcher("品牌:JYB", rules=rule_repo).match("JYB", field="品牌")
        assert result.mode == MATCH_EXACT
        assert result.corrected_from == ""

    def test_unrelated_value_no_fuzzy(self, rule_repo) -> None:
        """毫不相干的识别值**不得**因容错被放行。"""
        assert matcher("品牌:ZZZZ", rules=rule_repo).match("JYB", field="品牌").mode == MATCH_NONE


# ══════════════════════════════════════════════════════════════════
#  ④ 边界与空态
# ══════════════════════════════════════════════════════════════════
class TestEdgeCases:
    """空值 / 「无」/ 过短 / 无文本 —— 一律 ``NONE`` 并给出可解释说明。"""

    @pytest.mark.parametrize("declared", ["", "   ", None])
    def test_empty_declared(self, declared) -> None:
        result = matcher("品牌:JYB").match(declared, field="品牌")
        assert result.mode == MATCH_NONE
        assert "申报值为空" in result.note

    @pytest.mark.parametrize("declared", ["无", "N/A", "NONE", "无品"])
    def test_none_token_declared(self, declared: str) -> None:
        result = matcher("品牌:JYB").match(declared, field="品牌")
        assert result.mode == MATCH_NONE
        assert "视为无" in result.note

    def test_single_char_declared_rejected(self) -> None:
        result = matcher("品牌:A").match("A", field="品牌")
        assert result.mode == MATCH_NONE
        assert "长度 < 2" in result.note

    def test_empty_texts(self) -> None:
        assert TokenMatcher([], rules=None).match("JYB", field="品牌").mode == MATCH_NONE

    def test_no_rules_falls_back(self) -> None:
        """``rules=None`` 时用 constants 兜底，不得抛异常。"""
        assert matcher("品牌:JYB").match("JYB", field="品牌").mode == MATCH_EXACT

    def test_none_entries_filtered(self) -> None:
        """``texts`` 含 ``None`` 时被过滤，不炸。"""
        texts = [None, make_ocr("品牌:JYB", seq=2)]
        assert TokenMatcher(texts).match("JYB", field="品牌").images == [2]

    def test_missing_seq_is_safe(self) -> None:
        """``OcrText.seq`` 缺失/非法时不得抛异常。"""
        text = OcrText(image_path="x.jpg", text_raw="品牌:JYB")
        object.__setattr__(text, "seq", None)
        assert TokenMatcher([text]).match("JYB", field="品牌").mode == MATCH_EXACT


class TestTokenMatchModel:
    """``TokenMatch`` 数据契约。"""

    def test_hit_property(self) -> None:
        assert TokenMatch(mode=MATCH_EXACT).hit is True
        assert TokenMatch(mode=MATCH_FUZZY).hit is True
        assert TokenMatch(mode=MATCH_NONE).hit is False

    def test_to_dict_roundtrip_keys(self) -> None:
        data = TokenMatch(
            field="品牌",
            declared="JYB",
            mode=MATCH_EXACT,
            token="JYB",
            images=[1, 3],
            sample_line="品牌:JYB",
            note="n",
        ).to_dict()
        assert set(data) == {
            "field",
            "declared",
            "mode",
            "token",
            "images",
            "sample_line",
            "corrected_from",
            "note",
        }
        assert data["images"] == [1, 3]
