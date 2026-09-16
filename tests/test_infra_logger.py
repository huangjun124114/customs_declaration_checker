"""infra.logger 统一日志测试（T01 验收要点 ⑤ 相关）。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from infra.logger import (
    LogBus,
    LogLevel,
    Phase,
    PhaseLogger,
    get_log_bus,
    get_logger,
    set_log_bus,
    setup_logging,
)

#: 统一格式：HH:MM:SS LEVEL  [阶段] 消息
LOG_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2} (DEBUG|INFO|WARN |ERROR) +\[.+\] .+")


class TestSetupLogging:
    """日志初始化。"""

    def test_creates_file_and_parents(self, tmp_path: Path) -> None:
        log_file = tmp_path / "过程产出" / "校验运行日志_SA1.log"
        root = setup_logging(log_file, console=False)
        log = get_logger(Phase.PHASE1)
        log.info("探查 Excel 结构 → 变体 D")
        for handler in root.handlers:
            handler.flush()
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "变体 D" in content

    def test_format_matches_spec(self, tmp_path: Path) -> None:
        """格式必须为 ``HH:MM:SS LEVEL  [阶段] 消息``。"""
        log_file = tmp_path / "a.log"
        root = setup_logging(log_file, console=False)
        log = get_logger(Phase.PHASE1)
        log.info("测试消息")
        for handler in root.handlers:
            handler.flush()
        line = log_file.read_text(encoding="utf-8").strip()
        assert LOG_LINE_RE.match(line), f"格式不符：{line!r}"

    def test_idempotent_no_duplicate_handlers(self, tmp_path: Path) -> None:
        """重复调用不产生重复 handler（幂等）。"""
        root1 = setup_logging(tmp_path / "a.log", console=False)
        count1 = len(root1.handlers)
        root2 = setup_logging(tmp_path / "b.log", console=False)
        assert root1 is root2
        assert len(root2.handlers) == count1

    def test_console_channel_toggle(self, tmp_path: Path) -> None:
        """console=True 比 console=False 多一个控制台 handler。

        注意：``setup_logging`` 按 logger 名幂等，两次调用会先清空旧 handler，
        故需分别断言文件 handler 数量（用不同的 log 文件区分调用）。
        """
        root_console = setup_logging(tmp_path / "b.log", console=True, bus=LogBus())
        # 文件 + 控制台 + 信号 = 3
        assert len(root_console.handlers) == 3

        root_no_console = setup_logging(tmp_path / "a.log", console=False, bus=LogBus())
        # 文件 + 信号 = 2
        assert len(root_no_console.handlers) == 2
        assert root_no_console is root_console

    def test_file_records_debug_full(self, tmp_path: Path) -> None:
        """文件始终记全量（含 DEBUG）。"""
        log_file = tmp_path / "a.log"
        root = setup_logging(log_file, console=False, level=LogLevel.DEBUG)
        log = get_logger(Phase.APP)
        log.debug("调试细节")
        for handler in root.handlers:
            handler.flush()
        assert "调试细节" in log_file.read_text(encoding="utf-8")


class TestSignalChannel:
    """Qt 信号通道（回调注入，infra 不依赖 Qt）。"""

    def test_sink_receives_lines(self, tmp_path: Path) -> None:
        bus = LogBus()
        received: list[tuple[str, LogLevel]] = []
        bus.attach(lambda text, level: received.append((text, level)))

        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        get_logger(Phase.PHASE3).info("批量 OCR 完成")

        assert received
        text, level = received[-1]
        assert "批量 OCR 完成" in text
        assert "[Phase3]" in text
        assert level == LogLevel.INFO

    def test_level_mapping(self, tmp_path: Path) -> None:
        bus = LogBus()
        levels: list[LogLevel] = []
        bus.attach(lambda _text, level: levels.append(level))

        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        log = get_logger(Phase.APP)
        log.debug("d")
        log.info("i")
        log.warn("w")
        log.error("e")

        assert levels[-4:] == [
            LogLevel.DEBUG,
            LogLevel.INFO,
            LogLevel.WARN,
            LogLevel.ERROR,
        ]

    def test_faulty_sink_does_not_break(self, tmp_path: Path) -> None:
        """sink 抛异常不影响日志主流程。"""
        bus = LogBus()
        good: list[str] = []

        def bad_sink(_text, _level):
            raise RuntimeError("sink 故障")

        bus.attach(bad_sink)
        bus.attach(lambda text, _level: good.append(text))

        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        get_logger(Phase.APP).info("仍然要成功")
        assert any("仍然要成功" in t for t in good)

    def test_detach_stops_delivery(self, tmp_path: Path) -> None:
        bus = LogBus()
        received: list[str] = []
        sink = lambda text, _level: received.append(text)  # noqa: E731

        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        bus.attach(sink)
        get_logger(Phase.APP).info("before")
        bus.detach(sink)
        get_logger(Phase.APP).info("after")

        assert any("before" in t for t in received)
        assert not any("after" in t for t in received)

    def test_recent_backfill(self, tmp_path: Path) -> None:
        bus = LogBus()
        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        log = get_logger(Phase.APP)
        log.info("一")
        log.info("二")
        recent = bus.recent()
        assert any("二" in t for t, _ in recent)

    def test_recent_level_filter(self, tmp_path: Path) -> None:
        bus = LogBus()
        setup_logging(tmp_path / "a.log", console=False, bus=bus)
        log = get_logger(Phase.APP)
        log.debug("低")
        log.error("高")
        filtered = bus.recent(LogLevel.ERROR)
        assert all(level == LogLevel.ERROR for _t, level in filtered)
        assert any("高" in t for t, _ in filtered)


class TestPhaseLogger:
    """阶段前缀。"""

    def test_phase_enum_prefix(self, tmp_path: Path) -> None:
        bus = LogBus()
        captured: list[str] = []
        bus.attach(lambda text, _level: captured.append(text))
        setup_logging(tmp_path / "a.log", console=False, bus=bus)

        get_logger(Phase.EXPORT).info("导出完成")
        assert any("[导出]" in t for t in captured)

    def test_free_form_phase_string(self, tmp_path: Path) -> None:
        bus = LogBus()
        captured: list[str] = []
        bus.attach(lambda text, _level: captured.append(text))
        setup_logging(tmp_path / "a.log", console=False, bus=bus)

        get_logger("自定义阶段").info("x")
        assert any("[自定义阶段]" in t for t in captured)

    def test_is_logging_adapter(self) -> None:
        logger = get_logger(Phase.APP)
        assert isinstance(logger, PhaseLogger)
        assert isinstance(logger, logging.LoggerAdapter)


class TestGlobalBus:
    """进程级单例 bus。"""

    def test_get_set_bus(self) -> None:
        original = get_log_bus()
        replacement = LogBus()
        try:
            set_log_bus(replacement)
            assert get_log_bus() is replacement
        finally:
            set_log_bus(original)
