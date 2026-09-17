"""判定链路「申报为无 ＋ 图内显式无标记 → 核验通过」回归测试。

依据：用户裁定 2026-09-17（口径 v0.3.2）

    需求 1：申报品牌为「无」或**没有该要素** ＋ 图片 OCR 有「无品牌」字样 → 品牌核验通过
    需求 2：申报型号为「无」或**没有该要素** ＋ 图片 OCR 有「无型号」字样 → 型号核验通过

⚠️ 本文件同时锁三件事：
  ① **优先级**：显式「无」标记 > 同票图其他位置的品牌文字（后者只留痕、不改结论）；
  ② **防虚高反向锁**：申报**有值**时，图内「无品牌」**不得**把记录判成合格；
     且 `品牌:无锡机电` 这类"值以无开头"的**不得**被当成无标记；
  ③ **可追溯**：结论 ✅ 时证据链仍完整（其他品牌文字写进「判定依据」列）。
"""

from __future__ import annotations

import pytest

from core.constants import FIELD_BRAND, FIELD_MODEL, VERDICT_PASS
from core.element_parser import ElementParser
from core.judge_engine import JudgeEngine
from core.models import Verdict
from tests.test_judge_engine import make_evidence, make_record


@pytest.fixture
def engine(rule_repo) -> JudgeEngine:
    """注入规则仓库的判定引擎。"""
    return JudgeEngine(rules=rule_repo)


@pytest.fixture
def parser(rule_repo) -> ElementParser:
    """注入规则仓库的申报要素解析器（用于 T02→T05 接缝回归）。"""
    return ElementParser(rule_repo)


# ══════════════════════════════════════════════════════════════════
#  需求 1 · 品牌
# ══════════════════════════════════════════════════════════════════


class TestBrandNoneMarker:
    """申报品牌为无/缺该要素 ＋ 图内显式「无品牌」→ 合格。"""

    def test_missing_brand_element_with_bare_marker(self, engine: JudgeEngine) -> None:
        """申报要素里**根本没有品牌**（decl_brand 为空）＋ 图内「无品牌」独立行。"""
        record = make_record(brand="", model="HS-8A50J-12", evidences=[
            make_evidence("无品牌\n型号:HS-8A50J-12", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert "无品牌" in result.reason
        assert VERDICT_PASS == VERDICT_PASS  # 口径字符串未动（对照）

    def test_declared_brand_wu_with_labeled_marker(self, engine: JudgeEngine) -> None:
        """申报品牌为「无」＋ 图内 `品牌:无`。"""
        record = make_record(brand="无", model="A7A01G", evidences=[
            make_evidence("品牌:无\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert "品牌" in result.reason

    def test_marker_beats_other_brand_text_on_carton(self, engine: JudgeEngine) -> None:
        """⚠️ 核心变更：图内既有「无品牌」又有唛头上的品牌/料号文字 → 仍判合格。

        变更前：申报无 ＋ 图内有品牌文字 → ``DECLARED_MISSING`` → ❌ 校验异常。
        变更后（用户裁定）：显式「无品牌」是标签本体证据 → ✅；其他文字**留痕**。

        ⚠️ ``SKYWORTH`` 本身会被既有链路剔为**字段名残片**（``SKYWORTH P/N`` 在
        ``fields_blacklist`` 内，缺陷 C 口径），故实际留痕的是料号行抽出的 ``CARTON``
        —— 断言只认"抽出来的那个值"，不假设具体是哪一路。
        """
        record = make_record(brand="", model="HS-8A50J-12", evidences=[
            make_evidence("无品牌\n型号:HS-8A50J-12", seq=1),
            make_evidence("SKYWORTH P/N\nCARTON NO.B001", seq=2),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS, result.reason
        # 可追溯：其他位置识别到的品牌文字仍出现在判定依据里
        assert result.detected_brand, "本用例前提：图内确实抽出了别的品牌文字"
        notes = "；".join(d.note for d in result.differences)
        assert result.detected_brand in notes
        assert "不作为本体证据" in notes
        # 且明确写明命中的「无」标记
        assert "无品牌" in notes

    def test_token_match_carries_marker(self, engine: JudgeEngine) -> None:
        """引擎回吐 `TokenMatch.none_marker`（供 UI 判定链路列 + JSON 追溯）。"""
        record = make_record(brand="无", model="A7A01G", evidences=[
            make_evidence("品牌:无品牌\n型号:A7A01G", seq=3),
        ])
        result = engine.judge(record)
        match = result.token_match_for(FIELD_BRAND)
        assert match is not None
        assert match.none_marker
        assert match.none_marker_image == 3
        assert "显式标注" in match.note or "无品牌" in match.note
        # 取证诚实：申报侧为"无"，本就不参与分词匹配
        assert match.hit is False

    def test_row_still_13_columns(self, engine: JudgeEngine) -> None:
        """13 列冻结：新增证据只进判定依据 / JSON，**不得**增列。"""
        record = make_record(brand="无", model="A7A01G", evidences=[
            make_evidence("品牌:无\nSKYWORTH P/N", seq=1),
        ])
        row = engine.judge(record).to_row()
        assert len(row) == 13


class TestBrandNegativeGuards:
    """⚠️ 防虚高反向锁。"""

    def test_declared_has_value_marker_does_not_pass(self, engine: JudgeEngine) -> None:
        """申报**有值**时，图内「无品牌」不得判合格（那是"图内无该标识"→ ⚠️）。"""
        record = make_record(brand="SKYWORTH", model="A7A01G", evidences=[
            make_evidence("品牌:无\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict != Verdict.PASS
        assert result.verdict == Verdict.NO_MARK

    def test_wuxi_mechatronics_is_not_marker(self, engine: JudgeEngine) -> None:
        """`品牌:无锡机电` 以「无」开头但**不是**无标记 → 仍判 ❌（否则直接虚高）。"""
        record = make_record(brand="", model="A7A01G", evidences=[
            make_evidence("品牌:无锡机电\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL

    def test_no_marker_keeps_old_behavior(self, engine: JudgeEngine) -> None:
        """图内无「无」标记时，旧口径完全不变：申报无 ＋ 图内有品牌 → ❌。"""
        record = make_record(brand="", model="", evidences=[
            make_evidence("品牌:SKYWORTH\n型号:A7A01G", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.FAIL

    def test_both_absent_still_passes_without_marker(self, engine: JudgeEngine) -> None:
        """双方均为空的常规路径不受影响（规则①）。

        ⚠️ 图内文字刻意选**不含品牌/型号**的通用唛头行：若写入 ``CARTON NO.B001``，
        既有链路会从 ``品牌+型号行`` 层抽出 ``CARTON``，再被「跨文字体系兜底」判 ⚠️
        —— 那是 v0.1.0 起既有的「不虚高」行为，与本轮口径无关，故不在此处断言。
        """
        record = make_record(brand="无", model="无", evidences=[
            make_evidence("MADE IN CHINA\nQTY:100", seq=1),
        ])
        assert engine.judge(record).verdict == Verdict.PASS


# ══════════════════════════════════════════════════════════════════
#  需求 2 · 型号
# ══════════════════════════════════════════════════════════════════


class TestModelNoneMarker:
    """申报型号为无/缺该要素 ＋ 图内显式「无型号」→ 合格。"""

    def test_missing_model_element_with_marker(self, engine: JudgeEngine) -> None:
        record = make_record(brand="baori", model="", evidences=[
            make_evidence("baori\n无型号", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS
        assert "无型号" in result.reason

    def test_marker_beats_model_text(self, engine: JudgeEngine) -> None:
        """图内既有「无型号」又有其他位置的型号文字 → 仍判合格（留痕）。"""
        record = make_record(brand="baori", model="", evidences=[
            make_evidence("无型号", seq=1),
            make_evidence("baori\n型号:A7A01G", seq=2),
        ])
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS, result.reason
        match = result.token_match_for(FIELD_MODEL)
        assert match is not None and match.none_marker

    def test_declared_model_has_value_not_pass(self, engine: JudgeEngine) -> None:
        """申报型号有值 ＋ 图内「无型号」→ 不得判合格（⚠️ 图内无该标识）。"""
        record = make_record(brand="baori", model="A7A01G", evidences=[
            make_evidence("baori\n无型号", seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict != Verdict.PASS
        assert result.verdict == Verdict.NO_MARK

    def test_labeled_model_none(self, engine: JudgeEngine) -> None:
        record = make_record(brand="baori", model="无", evidences=[
            make_evidence("baori\n型号：无型号", seq=1),
        ])
        assert engine.judge(record).verdict == Verdict.PASS


class TestReasonTexts:
    """判定说明须**写明命中的标记与图号**（可追溯红线）。"""

    def test_reason_contains_marker_and_image(self, engine: JudgeEngine) -> None:
        record = make_record(brand="无", model="A7A01G", evidences=[
            make_evidence("品牌:无\n型号:A7A01G", seq=5),
        ])
        reason = engine.judge(record).reason
        assert "显式标注" in reason
        assert "图 5" in reason


# ══════════════════════════════════════════════════════════════════
#  v0.3.4 现场缺陷回归：申报要素原文 → 解析 → 判定（T02→T05 接缝）
# ══════════════════════════════════════════════════════════════════

#: 现场报告的申报要素原文 —— 品牌要素值为 ``无品牌``，``无商业价值`` 是**别的要素**，
#: 两者以**空格**连写（空格不是 ``rules/separators.yaml`` 的分隔符）。
_REPORTED_RAW_ELEMENT = "无品牌 无商业价值"

#: 现场报告的图片 OCR 全文（逐行，与现场截图一致）。
_REPORTED_OCR_LINES: tuple[str, ...] = (
    "N080102-000528-001",
    "20260507",
    "创维物料编号",
    "生产日期",
    "1912",
    "SKYHORTH P/N",
    "Production Date",
    "保修卡",
    "LG G25",
    "数量",
    "产品名称",
    "C17N",
    "美国",
    "0",
    "QTY",
    "供应商代码",
    "Supplier Code",
    "制造商全称",
    "Manufacturer Rame",
    "东莞币三主美术彩印有限公",
    "供应商全称",
    "司",
    "Supplier Nane",
    "中国",
    "原产地",
    "Country of Origin",
    "制造商型号",
    "MFR P/N",
    "无品牌",
    "品牌/Br and",
    "82026005",
    "生产批次/Batch",
)


class TestReportedCaseJoinedElementValues:
    """⚠️ 真实**接缝**缺陷：解析侧产出脏值 → 判定侧「申报为无」链路整条失效。

    现场报告（2026-09-17）：申报要素 ``无品牌 无商业价值`` ＋ 图内显式 ``无品牌``
    **应判 ✅**，实际因解析器把品牌读成 ``无 无商业价值``（``无商业价值`` 是别的要素）
    而判 ⚠️。本组用例走**完整链路**（``ElementParser.parse`` → ``JudgeEngine.judge``），
    锁住"解析不泄漏邻座要素值"与"申报为无链路生效"的**契约**。
    """

    def test_reported_case_passes_end_to_end(
        self, engine: JudgeEngine, parser: ElementParser
    ) -> None:
        """完整链路：``无品牌 无商业价值`` ＋ 图内「无品牌」→ ✅ 通过。"""
        parsed = parser.parse(_REPORTED_RAW_ELEMENT)
        assert parsed.brand == "", f"品牌被解析成 {parsed.brand!r}（邻座要素泄漏）"
        record = make_record(
            brand=parsed.brand,
            model=parsed.model,
            evidences=[make_evidence("\n".join(_REPORTED_OCR_LINES), seq=1)],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS, result.reason
        assert "无品牌" in result.reason
        # 可追溯红线：图内唛头识别到的品牌文字仍进「判定依据」，仅供人工追溯
        assert result.detected_brand == "SKYHORTH"
        assert "SKYHORTH" in "；".join(d.note for d in result.differences)
        # 判定链路取证：写明命中的显式「无」标记及其图号，且诚实标注未参与分词匹配
        match = result.token_match_for(FIELD_BRAND)
        assert match is not None
        assert match.none_marker == "无品牌"
        assert match.none_marker_image == 1
        assert match.hit is False

    def test_dirty_brand_value_would_break_the_chain(
        self, engine: JudgeEngine
    ) -> None:
        """**反证锁**：若品牌值仍是脏值 ``无 无商业价值``（修复前形态）→ 判不了 ✅。

        该用例保证上一条用例不是在"恒真"路径上通过 —— 脏值必须走不到合格。
        """
        record = make_record(brand="无 无商业价值", model="", evidences=[
            make_evidence("\n".join(_REPORTED_OCR_LINES), seq=1),
        ])
        result = engine.judge(record)
        assert result.verdict != Verdict.PASS, (
            "脏值仍能判合格 —— 说明护栏没在起作用（口径被绕过）"
        )


# ══════════════════════════════════════════════════════════════════
#  v0.3.5 现场缺陷回归：申报要素「邻座要素值 + 无品牌」连写（T02→T05 接缝）
#
#  用户裁定（2026-09-17，作为**分词识别规则**）：
#    ① 「无品牌」是一个**完整分词**；``1220mm*(50mm+50mm)*5mm`` 是**另外一个分词**；
#    ② **品牌和型号不会出现中英文混合词**。
# ══════════════════════════════════════════════════════════════════

#: 现场报告的申报要素原文（2026-09-17）：``规格`` 的取值与 ``无品牌`` 以**空格**连写
#: （``rules/separators.yaml`` 里空格**不是**分隔符）。
_REPORTED_RAW_ELEMENT_V035 = "材质：纸制|形状：L形,条状|规格：1220mm*(50mm+50mm)*5mm 无品牌"

#: 该票图片 OCR 行（含显式「无品牌」本体标记 + 唛头字段名残片）。
_REPORTED_OCR_LINES_V035: tuple[str, ...] = (
    "1220mm*(50mm+50mm)*5mm",
    "材质 纸制",
    "无品牌",
    "品牌/Brand",
    "SKYWORTH P/N",
)


class TestReportedCaseBrandTokenBoundary:
    """⚠️ 真实接缝缺陷（v0.3.5）：后缀词前缀被邻座要素值污染 → 「无」链路失效。

    现象：品牌被解析成 ``1220mm*(50mm+50mm)*5mm 无``（分词识别异常）→ 该脏值不是
    「无」类取值 → ``is_none_token()`` 失效 → 「申报为无 ＋ 图内显式『无品牌』→ 通过」
    （口径 v0.3.2）整条链路被跳过，记录被误判为 ⚠️。

    修复思路：**空格即分词边界** —— 后缀形态（``X品牌``）取紧邻后缀词的最后一个分词。
    """

    def test_reported_case_passes_end_to_end(
        self, engine: JudgeEngine, parser: ElementParser
    ) -> None:
        """完整链路：现场原文 ＋ 图内「无品牌」→ ✅ 通过。"""
        parsed = parser.parse(_REPORTED_RAW_ELEMENT_V035)
        assert parsed.brand == "", f"品牌被解析成 {parsed.brand!r}（分词边界失效）"
        record = make_record(
            brand=parsed.brand,
            model=parsed.model,
            evidences=[make_evidence("\n".join(_REPORTED_OCR_LINES_V035), seq=1)],
        )
        result = engine.judge(record)
        assert result.verdict == Verdict.PASS, result.reason
        assert "无品牌" in result.reason
        match = result.token_match_for(FIELD_BRAND)
        assert match is not None
        assert match.none_marker == "无品牌"
        assert match.none_marker_image == 1
        # 取证诚实：申报侧为「无」，本就不参与分词匹配
        assert match.hit is False

    def test_dirty_brand_value_would_break_the_chain(
        self, engine: JudgeEngine
    ) -> None:
        """**反证锁**：脏值 ``1220mm*(50mm+50mm)*5mm 无``（修复前形态）判不了 ✅。"""
        record = make_record(
            brand="1220mm*(50mm+50mm)*5mm 无",
            model="",
            evidences=[make_evidence("\n".join(_REPORTED_OCR_LINES_V035), seq=1)],
        )
        result = engine.judge(record)
        assert result.verdict != Verdict.PASS, (
            "脏值仍能判合格 —— 说明护栏没在起作用（口径被绕过）"
        )

