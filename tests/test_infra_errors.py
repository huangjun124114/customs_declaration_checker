"""infra.errors 异常体系测试（T01 验收要点 ⑦）。"""

from __future__ import annotations

from infra.errors import (
    FATAL_ERRORS,
    RECORD_ERRORS,
    CustomsCheckerError,
    ElementParseError,
    ErrorLevel,
    ExcelStructureError,
    OcrFailureError,
    OutputPathViolation,
    PathNotAccessibleError,
    RuleConfigError,
    TicketNoNotFoundError,
    is_fatal,
    user_message_of,
)


class TestHierarchy:
    """全部分支都继承基类。"""

    def test_all_subclass_base(self) -> None:
        for cls in (
            PathNotAccessibleError,
            ExcelStructureError,
            ElementParseError,
            OcrFailureError,
            OutputPathViolation,
            TicketNoNotFoundError,
            RuleConfigError,
        ):
            assert issubclass(cls, CustomsCheckerError)


class TestLevels:
    """分级策略（架构设计 9.5）。"""

    def test_path_error_is_fatal(self) -> None:
        exc = PathNotAccessibleError(path=r"\\host\x")
        assert exc.level == ErrorLevel.FATAL
        assert is_fatal(exc) is True

    def test_excel_structure_is_fatal(self) -> None:
        assert ExcelStructureError(path="a.xlsx").level == ErrorLevel.FATAL

    def test_ticket_not_found_is_fatal(self) -> None:
        assert TicketNoNotFoundError().level == ErrorLevel.FATAL

    def test_rule_config_is_fatal(self) -> None:
        assert RuleConfigError(path="r.yaml", field="terms").level == ErrorLevel.FATAL

    def test_element_parse_is_record(self) -> None:
        exc = ElementParseError(seq=3, part_no="P1")
        assert exc.level == ErrorLevel.RECORD
        assert is_fatal(exc) is False

    def test_ocr_failure_is_record(self) -> None:
        assert OcrFailureError(path="x.jpg").level == ErrorLevel.RECORD

    def test_output_violation_is_redline(self) -> None:
        exc = OutputPathViolation(path="x")
        assert exc.level == ErrorLevel.REDLINE
        assert is_fatal(exc) is True

    def test_unknown_exception_is_fatal(self) -> None:
        """未知异常一律按致命处理（宁可停下）。"""
        assert is_fatal(ValueError("boom")) is True


class TestUserMessage:
    """用户友好中文文案映射。"""

    def test_path_error_formats_path(self) -> None:
        exc = PathNotAccessibleError(path=r"\\172.20.99.220\share")
        assert r"\\172.20.99.220\share" in exc.user_message
        assert "共享盘" in exc.user_message

    def test_excel_error_mentions_file(self) -> None:
        exc = ExcelStructureError(path="D:\\a\\b.xlsx")
        assert "D:\\a\\b.xlsx" in exc.user_message

    def test_ticket_error_asks_manual_input(self) -> None:
        assert "手动填写" in TicketNoNotFoundError().user_message

    def test_element_parse_formats_context(self) -> None:
        exc = ElementParseError(seq=7, part_no="N0119")
        assert "7" in exc.user_message
        assert "N0119" in exc.user_message

    def test_ocr_error_formats_path(self) -> None:
        exc = OcrFailureError(path="D:\\图\\a.jpg")
        assert "D:\\图\\a.jpg" in exc.user_message

    def test_rule_config_error_has_field(self) -> None:
        exc = RuleConfigError(path="noise_signals.yaml", field="confusable_chars")
        assert "noise_signals.yaml" in exc.user_message
        assert "confusable_chars" in exc.user_message

    def test_all_messages_are_chinese_and_non_empty(self) -> None:
        for cls in (
            PathNotAccessibleError,
            ExcelStructureError,
            TicketNoNotFoundError,
            OutputPathViolation,
            ElementParseError,
            OcrFailureError,
            RuleConfigError,
        ):
            msg = cls().user_message
            assert msg and any("\u4e00" <= ch <= "\u9fa5" for ch in msg), cls.__name__


class TestHelpers:
    """模块级辅助函数。"""

    def test_user_message_of_business_exception(self) -> None:
        exc = PathNotAccessibleError(path="x")
        assert user_message_of(exc) == exc.user_message

    def test_user_message_of_unknown_exception(self) -> None:
        msg = user_message_of(RuntimeError("boom"))
        assert "RuntimeError" in msg

    def test_fatal_and_record_sets_disjoint(self) -> None:
        assert not (set(FATAL_ERRORS) & set(RECORD_ERRORS))

    def test_custom_user_message_override(self) -> None:
        exc = CustomsCheckerError("tech", user_message="自定义文案")
        assert exc.user_message == "自定义文案"

    def test_context_preserved(self) -> None:
        exc = ElementParseError(seq=1, part_no="P", note="x")
        assert exc.context["seq"] == 1
        assert exc.context["part_no"] == "P"
