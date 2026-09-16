"""``core.element_parser`` 回归测试（T02 验收要点 ④⑤⑥⑦ + 契约"永不抛异常"）。

覆盖：
  * 五种品牌形态（``品牌:baori`` / ``品牌:无`` / ``无品牌`` / ``宇同品牌`` / ``品牌;无;``）；
  * 型号脏尾 ``A7A01G ，电视机用/`` → ``A7A01G``，且 ``2.402GHz`` **不被误伤**；
  * 零宽字符 ``\\u200b`` 等被剥离；
  * **SOP 3.5 八条防误提取规则**各有对应用例；
  * **契约**：``parse`` **永不抛异常**；
  * **规则外置一致性**：参数化回归集断言"从 YAML 加载的规则"解析结果与期望一致
    （架构设计 v1.1 C.4：外置前后行为一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.element_parser import (
    ElementParser,
    clean_model_tail,
    is_blacklisted,
    normalize_separators,
    split_fields,
    strip_invisible,
)
from core.rule_repository import RuleRepository

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_ELEMENT_FIXTURE = _FIXTURES_DIR / "element_samples.json"


def _fixture() -> dict:
    """加载解析回归集。"""
    return json.loads(_ELEMENT_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def parser(rule_repo: RuleRepository) -> ElementParser:
    """注入内置规则仓库的解析器（外置规则路径）。"""
    return ElementParser(rule_repo)


# ══════════════════════════════════════════════════════════════════
#  ④ 五种品牌形态（T02 验收要点 ④）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected_brand"),
    [
        ("品牌:baori|型号:A7A01G ，电视机用/", "baori"),
        ("品牌:无|型号:无", ""),
        ("无品牌|型号:HS-8A50J-12  蓝牙遥控器", ""),
        ("宇同品牌、型号：YT-100", "宇同"),
        ("品牌;无;|型号:GXD-009;", ""),
    ],
    ids=[
        "form1_brand_colon_ascii",
        "form2_brand_colon_none",
        "form3_no_brand_no_colon",
        "form4_chinese_suffix",
        "form5_semicolon_empty",
    ],
)
def test_five_brand_forms(parser: ElementParser, raw: str, expected_brand: str) -> None:
    """五种品牌形态全部正确（T02 验收要点 ④）。"""
    assert parser.parse(raw).brand == expected_brand


def test_brand_baori_exact(parser: ElementParser) -> None:
    """``品牌:baori`` → ``baori``（不得截成 ``baori|型号...``）。"""
    assert parser.parse("品牌:baori|型号:A7A01G ，电视机用/").brand == "baori"


def test_brand_yutong_chinese_suffix(parser: ElementParser) -> None:
    """``宇同品牌`` → ``宇同``（去掉"品牌"后缀）。"""
    parsed = parser.parse("宇同品牌、无型号、用途：电视机用；结构类型：有接头；额定电压：60V")
    assert parsed.brand == "宇同"
    assert parsed.model == ""


def test_brand_none_semicolon(parser: ElementParser) -> None:
    """``品牌;无;``（分号夹空）→ 无品牌，且型号仍能取出。"""
    parsed = parser.parse("电视机用;类型：直流稳压电源;功率：300W 精度± 10%；品牌;无;型号:L8M")
    assert parsed.brand == ""
    assert parsed.model == "L8M"


# ══════════════════════════════════════════════════════════════════
#  ⑤ 型号脏尾清洗（T02 验收要点 ⑤）
# ══════════════════════════════════════════════════════════════════


def test_model_dirty_tail_cleaned(parser: ElementParser) -> None:
    """``A7A01G ，电视机用/`` → ``A7A01G``（T02 验收要点 ⑤）。"""
    parsed = parser.parse("品牌：无/型号:A7A01G ，电视机用/适用机型:32DA25QL")
    assert parsed.model == "A7A01G"


def test_model_ghz_not_harmed(parser: ElementParser) -> None:
    """``2.402GHz`` **不得**被误伤（核心约束：绝不可贪婪截断）。"""
    assert parser.parse("品牌:COOCAA|型号:2.402GHz").model == "2.402GHz"


def test_model_watt_not_harmed() -> None:
    """``300W`` 功率数值不得被截断。"""
    assert clean_model_tail("300W") == "300W"


def test_model_dirty_tail_multiple_separators() -> None:
    """多种脏尾分隔符（逗号/斜杠/分号/括号）均正确截断。"""
    assert clean_model_tail("A7A01G，电视机用") == "A7A01G"
    assert clean_model_tail("A7A01G/适用机型") == "A7A01G"
    assert clean_model_tail("A7A01G;备注") == "A7A01G"
    assert clean_model_tail("HS-8A50J-12  ") == "HS-8A50J-12"


def test_model_empty_and_none(parser: ElementParser) -> None:
    """``型号:无`` / 无型号 → 空。"""
    assert parser.parse("品牌:无|型号:无").model == ""
    assert parser.parse("无品牌、无型号、用途：电视机用").model == ""
    assert clean_model_tail("") == ""


# ══════════════════════════════════════════════════════════════════
#  ⑥ 零宽字符剥离（T02 验收要点 ⑥）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "invisible",
    ["\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u3000"],
)
def test_zero_width_stripped(invisible: str) -> None:
    """各类不可见字符均被剥离（``\\s`` 不匹配 ``\\u200b``）。"""
    assert strip_invisible(f"abc{invisible}def") == "abcdef"


def test_zero_width_in_brand(parser: ElementParser) -> None:
    """``品牌:COOCAA\\u200b`` → ``COOCAA``（T02 验收要点 ⑥）。"""
    assert parser.parse("品牌:COOCAA\u200b|型号:COOM-55K").brand == "COOCAA"


def test_zero_width_mixed_all(parser: ElementParser) -> None:
    """四种零宽字符混合污染仍能正确解析。"""
    parsed = parser.parse("品牌\u200c:\u200dYL\u200bO\u200dO\ufeff|型号:XY-1")
    assert parsed.brand == "YLOO"
    assert parsed.model == "XY-1"


# ══════════════════════════════════════════════════════════════════
#  ⑦ 八条防误提取规则（T02 验收要点 ⑦）
# ══════════════════════════════════════════════════════════════════


def test_rule1_field_name_not_taken_as_value(parser: ElementParser) -> None:
    """规则#1：字段名不得被当作值（``品牌:MFR P/N`` → 空）。"""
    assert parser.parse("品牌:MFR P/N|型号:A7A01G").brand == ""


def test_rule2_field_value_window(parser: ElementParser) -> None:
    """规则#2：字段名前后窗口搜索有效值（值在后续段落仍能取到）。"""
    parsed = parser.parse("品牌:无；型号:A7A01G；适用机型:32DA25QL")
    assert parsed.model == "A7A01G"


def test_rule3_field_name_blacklist_word(parser: ElementParser) -> None:
    """规则#3：字段名黑名单（``创维物料编号`` 实测复现，13.5）。"""
    assert parser.parse("品牌:创维物料编号|型号:N011901-007386-001").brand == ""


def test_rule4_zero_width(parser: ElementParser) -> None:
    """规则#4：零宽字符剥离（见"⑥ 零宽字符"用例）。"""
    assert parser.parse("品牌:COOCAA\u200b").brand == "COOCAA"


def test_rule5_context_skip_prefix(parser: ElementParser) -> None:
    """规则#5：语境排除——「适用于 PHILIPS」不是申报品牌。"""
    assert parser.parse("品牌:适用于PHILIPS电视机|型号:55PUF").brand == ""


def test_rule6_brand_patterns(parser: ElementParser) -> None:
    """规则#6：多形态品牌正则（ASCII/中文/单"牌"字）。"""
    assert parser.parse("品牌:COOCAA").brand == "COOCAA"
    assert parser.parse("品牌：宇同").brand == "宇同"
    assert parser.parse("SAMSUNG牌|型号:UA55").brand == "SAMSUNG"


def test_rule7_model_head_anchor(parser: ElementParser) -> None:
    """规则#7：型号头部锚定（见"⑤ 型号脏尾"用例）。"""
    assert parser.parse("型号:A7A01G ，电视机用/").model == "A7A01G"


def test_rule8_whole_machine_brand_is_not_parser_concern(parser: ElementParser) -> None:
    """规则#8：外箱整机品牌由 NoiseGuard/JudgeEngine 处置，解析器只负责取值。

    本用例断言解析器**不改写口径**：``Brand:Daewoo`` 仍解析为品牌值
    （是否"整机品牌、不构成本体证据"由 T04 的 NoiseGuard 依上下文判定）。
    """
    parsed = parser.parse("Brand:Daewoo|CARTON|JOBNO")
    assert parsed.brand == "Daewoo"


def test_blacklist_helper_direct() -> None:
    """``is_blacklisted`` 直接命中黑名单词与语境前缀。"""
    assert is_blacklisted("制造商全称", terms=["制造商全称"]) is True
    assert is_blacklisted("COOCAA", terms=["制造商全称"]) is False
    assert is_blacklisted("PHILIPS", context="适用于PHILIPS", skip_prefixes=["适用于"]) is True


# ══════════════════════════════════════════════════════════════════
#  分隔符归一化 + 字段拆分
# ══════════════════════════════════════════════════════════════════


def test_normalize_separators_mixed() -> None:
    """``|`` / ``、`` / ``;`` 与 ``:`` / ``：`` 混合归一化。"""
    result = normalize_separators("品牌：无、型号;A7A01G；用途:电视机用")
    assert "|" in result
    assert "：" not in result
    assert "、" not in result


def test_split_fields_tolerates_no_colon() -> None:
    """无冒号段落按单 token 处理（``无品牌`` / ``宇同品牌``）。"""
    pairs = split_fields("无品牌|宇同品牌|品牌:baori")
    assert ("无品牌", "无品牌") in pairs
    assert ("宇同品牌", "宇同品牌") in pairs
    assert ("品牌", "baori") in pairs


# ══════════════════════════════════════════════════════════════════
#  契约：parse 永不抛异常
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "   ",
        "!!!@@@###$$$",
        "品牌" * 500,
        "\u200b\u200c\u200d\ufeff",
        "品牌:\x00\x01|型号:\x02",
        "::::||||;;;;",
        "品牌:🎉🎉🎉",
    ],
)
def test_parse_never_raises(parser: ElementParser, raw: object) -> None:
    """**契约**：``parse`` 永不抛异常，最坏返回空 brand/model。"""
    parsed = parser.parse(raw)  # type: ignore[arg-type]
    assert isinstance(parsed.brand, str)
    assert isinstance(parsed.model, str)


def test_parse_returns_parsed_element_type(parser: ElementParser) -> None:
    """返回类型为 :class:`core.models.ParsedElement`，含 fields 调试信息。"""
    from core.models import ParsedElement

    parsed = parser.parse("品牌:baori|型号:A7A01G")
    assert isinstance(parsed, ParsedElement)
    assert parsed.fields
    assert parsed.to_dict()["brand"] == "baori"


# ══════════════════════════════════════════════════════════════════
#  口径 v0.3.1：只有「品牌」「型号」参与识别与判定
# ══════════════════════════════════════════════════════════════════


def test_scope_only_brand_and_model_fields_are_used(parser: ElementParser) -> None:
    """其他要素（用途 / 结构类型 / 额定电压 / 长度）**不得**影响 brand / model。"""
    raw = (
        "用途:电视机用|结构类型:有接头|品牌:baori|型号:A7A01G|"
        "额定电压:60V|长度:150MM|材质:铜"
    )
    parsed = parser.parse(raw)
    assert parsed.brand == "baori"
    assert parsed.model == "A7A01G"


def test_scope_other_elements_only_yields_empty(parser: ElementParser) -> None:
    """**只有其他要素、没有品牌/型号** → 两个结果都为空，绝不"就近取值"。"""
    raw = "用途:电视机用|结构类型:有接头|额定电压:60V|长度:150MM"
    parsed = parser.parse(raw)
    assert parsed.brand == ""
    assert parsed.model == ""


@pytest.mark.parametrize(
    "raw",
    [
        # 报关要素「0:品牌类型」——取值为品牌**归类**，不是品牌本身
        "品牌类型:0|用途:电视机用|型号:A7A01G",
        "品牌类型:1|品牌:baori|型号:A7A01G",
        "商标类型:0|品牌:baori|型号:无",
        "品牌种类:0|用途:电视机用|型号:无",
    ],
)
def test_scope_brand_type_never_read_as_brand(parser: ElementParser, raw: str) -> None:
    """``品牌类型`` 这类**非值类**要素字段不得被读成品牌值。

    ⚠️ 缺此护栏时 ``品牌类型:0`` 会被读成品牌 ``0``（直接违反「不虚高」红线）。
    """
    parsed = parser.parse(raw)
    assert parsed.brand != "0"


def test_scope_model_type_never_read_as_model(parser: ElementParser) -> None:
    """``型号类型`` 同样不得被读成型号值。"""
    parsed = parser.parse("品牌:baori|型号类型:0|用途:电视机用")
    assert parsed.model != "0"


def test_scope_brand_type_with_real_brand_still_works(parser: ElementParser) -> None:
    """排除「品牌类型」**不能误伤**同段里的真实品牌字段。"""
    parsed = parser.parse("品牌类型:1|品牌:baori|型号:A7A01G")
    assert parsed.brand == "baori"
    assert parsed.model == "A7A01G"


def test_scope_no_colon_brand_value_form_still_works(parser: ElementParser) -> None:
    """无冒号的品牌**取值形态**（``宇同品牌`` / ``无品牌``）**不受口径收窄影响**。"""
    assert parser.parse("宇同品牌、无型号、用途：电视机用").brand == "宇同"
    assert parser.parse("无品牌、无型号、用途：电视机用").brand == ""


def test_scope_spec_model_key_still_recognized(parser: ElementParser) -> None:
    """``规格型号`` / ``产品型号`` 仍是合法型号字段（收窄不得误伤）。"""
    assert parser.parse("品牌:baori|规格型号:A7A01G").model == "A7A01G"
    assert parser.parse("品牌:baori|产品型号:A7A01G").model == "A7A01G"


def test_scope_helper_excludes_non_value_keys(parser: ElementParser) -> None:
    """``_is_non_value_key`` 判定表（护栏自证）。"""
    assert parser._is_non_value_key("品牌类型") is True  # noqa: SLF001
    assert parser._is_non_value_key("型号类型") is True  # noqa: SLF001
    assert parser._is_non_value_key("品牌") is False  # noqa: SLF001
    assert parser._is_non_value_key("规格型号") is False  # noqa: SLF001
    assert parser._is_non_value_key("") is False  # noqa: SLF001


def test_scope_brand_key_rejects_brand_type(parser: ElementParser) -> None:
    """``_is_brand_key`` 必须拒收「品牌类型」，但保留「品牌」「品牌名称」等值类字段。"""
    assert parser._is_brand_key("品牌") is True  # noqa: SLF001
    assert parser._is_brand_key("品牌名称") is True  # noqa: SLF001
    assert parser._is_brand_key("BRAND") is True  # noqa: SLF001
    assert parser._is_brand_key("品牌类型") is False  # noqa: SLF001
    assert parser._is_brand_key("商标类型") is False  # noqa: SLF001


# ══════════════════════════════════════════════════════════════════
#  规则外置一致性（v1.1 C.4）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "case",
    _fixture()["cases"],
    ids=[case["name"] for case in _fixture()["cases"]],
)
def test_externalized_rules_regression(parser: ElementParser, case: dict) -> None:
    """外置规则回归集参数化：从 ``rules/*.yaml`` 加载的规则解析结果与期望一致。

    这是"外置前后行为一致"的机器可验证断言（架构设计 12.C.4）。
    """
    parsed = parser.parse(case["raw"])
    assert parsed.brand == case["brand"], (
        f"{case['name']}（{case['rule']}）品牌期望 {case['brand']!r}，实际 {parsed.brand!r}"
    )
    assert parsed.model == case["model"], (
        f"{case['name']}（{case['rule']}）型号期望 {case['model']!r}，实际 {parsed.model!r}"
    )


def test_parser_uses_repository_rules(rule_repo: RuleRepository) -> None:
    """解析器**确实从注入的 RuleRepository 读规则**（非硬编码）。"""
    from core.element_parser import describe_rules

    parser = ElementParser(rule_repo)
    summary = describe_rules(parser.rules)
    assert summary["brand_patterns"] > 0
    assert summary["blacklist_terms"] > 0
    assert summary["separators"] > 0
    assert summary["invisible_chars"] > 0


def test_parser_default_constructs_from_repo(rules_dir: Path) -> None:
    """不注入仓库时，解析器默认从工程 ``rules/`` 加载（可用性断言）。"""
    repo = RuleRepository(builtin_dir=rules_dir)
    parser = ElementParser(repo)
    assert parser.parse("品牌:baori").brand == "baori"


# ══════════════════════════════════════════════════════════════════
#  缺陷 E 回归锁（架构裁决文档 §5A）：skip_prefixes 不得误伤型号
#
#  语义对齐 SOP 陷阱 #5 原文「捕获词**前**若有 适用于/用于 → 跳过」：
#    * `is_blacklisted` 的 `skip_prefixes` 由「全串包含」改「**捕获词前 N=8 字符窗口**」；
#    * 型号字段（`_clean_model`）**`skip_prefixes` 置空**（仅保留 terms）。
#  实测根因：`_clean_model(cleaned, value)` 的 `value` 含「适用于DAEWOO牌电视机」
#  → 全串包含命中「适用于」→ 型号 `HS-8A50J-12` 被误判空（seq=16/17/18）。
# ══════════════════════════════════════════════════════════════════

_SEQ16_RAW = (
    "用途:电视机；功能:蓝牙遥控器；品牌:DAEWOO；型号：HS-8A50J-12  "
    "蓝牙遥控器，工作频率2.402GHz，遥控距离6米，遥控方式：蓝牙+红外，"
    "供电方式两节7号电池，有语音，ABS外壳，适用于DAEWOO牌电视机"
)


def test_model_not_killed_by_shiyongyu_context(parser: ElementParser) -> None:
    """缺陷 E：型号尾部含『适用于X牌』不得致型号判空（SOP 陷阱#5 语义=捕获词前）。

    实测锚点（seq=16/17/18）：修复前 ``model == ''``（型号丢失），修复后应为
    ``HS-8A50J-12``。
    """
    parsed = parser.parse(_SEQ16_RAW)
    assert parsed.brand == "DAEWOO"
    assert parsed.model == "HS-8A50J-12", (
        f"缺陷 E：型号 HS-8A50J-12 不得因尾部『适用于DAEWOO牌电视机』被判空，"
        f"实际 {parsed.model!r}"
    )


def test_brand_still_guarded_by_shiyongyu(parser: ElementParser) -> None:
    """品牌语境排除仍生效：『适用于PHILIPS电视机』不判 PHILIPS 为申报品牌。"""
    parsed = parser.parse("适用于PHILIPS电视机")
    assert parsed.brand != "PHILIPS", "SOP 陷阱#5：『适用于X』是适配对象，非申报品牌"


def test_is_blacklisted_skip_prefix_window(parser: ElementParser) -> None:
    """``is_blacklisted`` 前缀窗口语义：候选词**前**紧邻含『适用于』才判空。

    * ``context='适用于DAEWOO' value='DAEWOO'`` → 命中（紧邻在前）；
    * ``context='...适用于DAEWOO牌电视机' value='HS-8A50J-12'`` → **不命中**（远距）。
    """
    assert is_blacklisted("DAEWOO", skip_prefixes=("适用于",), context="适用于DAEWOO") is True
    assert (
        is_blacklisted(
            "HS-8A50J-12",
            skip_prefixes=("适用于",),
            context=_SEQ16_RAW,
        )
        is False
    ), "缺陷 E：型号与『适用于』相距 > 8 字符，不得命中前缀排除"


def test_clean_model_tail_still_correct(parser: ElementParser) -> None:
    """陷阱#7 型号脏尾清洗不受影响（``clean_model_tail`` 本身正确）。"""
    cleaned = clean_model_tail(
        "HS-8A50J-12  蓝牙遥控器，工作频率2.402GHz",
        dirty_tail_chars=parser.rules.model_clean_rules.dirty_tail_chars,
        protect_patterns=parser.rules.model_clean_rules.protect_patterns,
        anchor_regex=parser.rules.model_clean_rules.anchor_regex,
    )
    assert cleaned == "HS-8A50J-12"
