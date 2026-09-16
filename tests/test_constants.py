"""口径常量测试（T01 验收要点 ④）。

**核心断言**：``core/constants.py`` 的四类判定字符串必须与 SOP 1.3 **逐字一致**。
若本测试失败，说明口径被改动 —— 必须回到 SOP 复核并由用户确认（SOP 8.4 护栏#1）。
"""

from __future__ import annotations

from core import constants as C
from core.models import NoiseLevel, Verdict


class TestVerdictStrings:
    """四类判定字符串与 SOP 1.3 逐字一致。"""

    #: SOP 1.3 原文（照抄，作为独立事实来源；**不得**从 constants 推导）
    SOP_PASS = "✅ 校验合格"
    SOP_FAIL = "❌ 校验异常"
    SOP_NO_MARK = "⚠️ 缺图内标识，人工复核"
    SOP_NO_IMAGE = "🔵 缺图，人工复核"

    def test_pass_exact(self) -> None:
        assert C.VERDICT_PASS == self.SOP_PASS

    def test_fail_exact(self) -> None:
        assert C.VERDICT_FAIL == self.SOP_FAIL

    def test_no_mark_exact(self) -> None:
        assert C.VERDICT_NO_MARK == self.SOP_NO_MARK

    def test_no_image_exact(self) -> None:
        assert C.VERDICT_NO_IMAGE == self.SOP_NO_IMAGE

    def test_verdict_text_map_complete(self) -> None:
        """枚举 → 口径字符串映射覆盖全部四类。"""
        assert set(C.VERDICT_TEXT.keys()) == set(Verdict)
        assert C.VERDICT_TEXT[Verdict.PASS] == self.SOP_PASS
        assert C.VERDICT_TEXT[Verdict.FAIL] == self.SOP_FAIL
        assert C.VERDICT_TEXT[Verdict.NO_MARK] == self.SOP_NO_MARK
        assert C.VERDICT_TEXT[Verdict.NO_IMAGE] == self.SOP_NO_IMAGE

    def test_verdict_text_helper_accepts_str_and_enum(self) -> None:
        assert C.verdict_text(Verdict.NO_MARK) == self.SOP_NO_MARK
        assert C.verdict_text("NO_MARK") == self.SOP_NO_MARK

    def test_labels_complete(self) -> None:
        assert set(C.VERDICT_LABELS.keys()) == set(Verdict)

    def test_all_verdicts_order_fixed(self) -> None:
        """UI 四卡顺序固定：合格 → 异常 → 缺标识 → 缺图。"""
        assert C.ALL_VERDICTS == (
            Verdict.PASS,
            Verdict.FAIL,
            Verdict.NO_MARK,
            Verdict.NO_IMAGE,
        )

    def test_review_verdicts(self) -> None:
        """待人工复核清单仅含 ⚠️ + 🔵。"""
        assert C.REVIEW_VERDICTS == (Verdict.NO_MARK, Verdict.NO_IMAGE)


class TestColumns:
    """汇总表 13 列（SOP 七）。"""

    def test_exactly_13_columns(self) -> None:
        assert len(C.COLUMNS) == 13
        assert C.COLUMN_COUNT == 13

    def test_first_three_are_index_keys(self) -> None:
        assert C.COLUMNS[:3] == ["出货通知书号", "成品料号", "订单号"]

    def test_no_duplicate_columns(self) -> None:
        assert len(set(C.COLUMNS)) == len(C.COLUMNS)


class TestDefaults:
    """默认兜底值（与 rules/*.yaml 同步，详见 test_rule_repository.py）。"""

    def test_field_blacklist_not_empty(self) -> None:
        assert C.DEFAULT_FIELD_BLACKLIST
        assert "制造商全称" in C.DEFAULT_FIELD_BLACKLIST
        assert "MFR P/N" in C.DEFAULT_FIELD_BLACKLIST

    def test_skip_prefixes(self) -> None:
        assert "适用于" in C.DEFAULT_SKIP_PREFIXES

    def test_confusable_chars_contains_measured_pairs(self) -> None:
        """v1.2 实测：W/H 与 N/H 必须在易混字符表内（SKYWORTH→SKYHORTH / P/N→P/H）。"""
        pairs = [set(group) for group in C.DEFAULT_CONFUSABLE_CHARS]
        assert {"W", "H"} in pairs
        assert {"N", "H"} in pairs
        assert {"O", "0"} in pairs
        assert {"I", "1", "l"} in pairs

    def test_fuzzy_params(self) -> None:
        assert C.DEFAULT_FUZZY_MAX_EDIT_DISTANCE == 2
        assert C.DEFAULT_FUZZY_MIN_LENGTH == 5

    def test_fuzzy_verdict_not_fail(self) -> None:
        """不虚高红线：模糊相似度命中必须降级 ⚠️，绝不直达 ❌。"""
        assert C.DEFAULT_FUZZY_VERDICT == NoiseLevel.SUSPICIOUS
        assert C.DEFAULT_FUZZY_VERDICT != NoiseLevel.CLEAR_MISMATCH

    def test_separators(self) -> None:
        assert "|" in C.DEFAULT_SEPARATORS
        assert "、" in C.DEFAULT_SEPARATORS
        assert ";" in C.DEFAULT_SEPARATORS

    def test_whole_machine_context(self) -> None:
        assert "Brand:" in C.DEFAULT_WHOLE_MACHINE_CONTEXT
        assert "CARTON" in C.DEFAULT_WHOLE_MACHINE_CONTEXT

    def test_ticket_pattern_tolerates_variants(self) -> None:
        """票号正则须容忍「出货通知书号 / 出货通知号」+ 有/无冒号。"""
        import re

        pattern = re.compile(C.TICKET_NO_PATTERN)
        assert pattern.search("出货通知书号:SA26090215").group("ticket") == "SA26090215"
        assert pattern.search("出货通知号SA26090215").group("ticket") == "SA26090215"
        assert pattern.search("出货通知书号：SA26090215").group("ticket") == "SA26090215"
