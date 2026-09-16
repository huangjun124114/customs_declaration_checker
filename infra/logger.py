"""统一日志工厂（infra.logger，对应架构设计 9.3）。

职责：
  * **双通道**日志：
      ① 滚动文件 —— 落 ``过程产出/校验运行日志_{票号}.log``，**始终记全量**；
      ② Qt 信号 —— ``log_emitted(str, LogLevel)`` 供 UI 日志区滚动（UI 侧做级别过滤，不丢日志）。
  * **统一格式**：``HH:MM:SS LEVEL  [阶段] 消息``
      例：``12:00:01 INFO   Phase1 探查 Excel 结构 → 变体 D，要素表=Sheet2``
  * **禁止**打印完整 OCR 原文（避免刷屏）；完整原文只进 JSON 证据文件。

设计取舍：本模块**不直接 import PySide6**（保持 infra 层零 GUI 依赖）。
Qt 信号通道通过「回调注入」实现 —— :func:`attach_signal_sink` 接收一个
``Callable[[str, LogLevel], None]``，由 UI 层（L4）在启动时把自己的 Qt 信号
``emit`` 方法传进来。这样 infra 层无需知道 Qt 的存在。
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from infra.encoding import normalize_path
from infra.encoding import write_text as _write_text  # noqa: F401  (保留导出语义)

__all__ = [
    "LogLevel",
    "Phase",
    "LogBus",
    "setup_logging",
    "get_logger",
    "get_log_bus",
    "set_log_bus",
    "attach_signal_sink",
    "detach_signal_sink",
    "LOG_FORMAT",
]


class LogLevel(str, Enum):
    """日志级别（与架构设计 4 节类图 LogLevel 枚举对齐）。

    ``core.models.LogLevel`` 在 L2 层定义；本层独立定义同名枚举以保持
    infra 零上层依赖。两者取值一一对应，由 ``ui``/``app`` 层做显式转换。
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"

    @property
    def py_level(self) -> int:
        """映射到 Python :mod:`logging` 数值级别。"""
        return {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARN: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
        }[self]


class Phase(str, Enum):
    """SOP 8 Phase 阶段标签（日志前缀 ``[阶段]``）。"""

    APP = "APP"
    PHASE0 = "Phase0"
    PHASE1 = "Phase1"
    PHASE2 = "Phase2"
    PHASE3 = "Phase3"
    PHASE4 = "Phase4"
    PHASE5 = "Phase5"
    PHASE6 = "Phase6"
    PHASE7 = "Phase7"
    PHASE8 = "Phase8"
    OCR = "OCR"
    IO = "IO"
    EXPORT = "导出"


#: 统一日志格式：``HH:MM:SS LEVEL  [阶段] 消息``
_LOG_FORMAT = "%(asctime)s %(levelname)-5s  %(message)s"
_DATE_FORMAT = "%H:%M:%S"
LOG_FORMAT = _LOG_FORMAT

#: 格式化后的日志行正则（供测试断言）：HH:MM:SS LEVEL  [阶段] 消息
_LEVEL_ORDER: dict[str, int] = {
    LogLevel.DEBUG.value: 0,
    LogLevel.INFO.value: 1,
    LogLevel.WARN.value: 2,
    LogLevel.ERROR.value: 3,
}


class _SignalBridge(logging.Handler):
    """把日志记录转发到已注册的"信号 sink"（Qt 侧的回调）。

    sink 签名：``Callable[[str, LogLevel], None]``，参数为「已格式化行, 级别」。
    """

    def __init__(self, bus: LogBus) -> None:
        super().__init__(level=logging.DEBUG)
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - 覆写
        try:
            level = _record_level(record)
            text = self.format(record)
            self._bus.publish(text, level)
        except Exception:  # noqa: BLE001 - 日志本身绝不能抛异常影响主流程
            self.handleError(record)


def _record_level(record: logging.LogRecord) -> LogLevel:
    """把 :class:`logging.LogRecord` 映射回业务 :class:`LogLevel`。"""
    if record.levelno >= logging.ERROR:
        return LogLevel.ERROR
    if record.levelno >= logging.WARNING:
        return LogLevel.WARN
    if record.levelno >= logging.INFO:
        return LogLevel.INFO
    return LogLevel.DEBUG


class LogBus:
    """日志总线：持有 sink 列表，向所有 sink 广播日志行。

    UI 层（L4）通过 :func:`attach_signal_sink` 注册自己的 Qt 信号 ``emit``；
    引擎层（L2/L3）无需知道 Qt。

    Thread-safe：内部用 ``threading.Lock`` 保护 sink 列表（OCR 在子线程 emit）。
    """

    def __init__(self) -> None:
        self._sinks: list[Callable[[str, LogLevel], None]] = []
        self._lock = threading.Lock()
        self._recent: list[tuple[str, LogLevel]] = []
        self._recent_limit = 500

    def attach(self, sink: Callable[[str, LogLevel], None]) -> None:
        """注册一个日志 sink。

        Args:
            sink: 回调，签名 ``(text: str, level: LogLevel) -> None``。
        """
        with self._lock:
            if sink not in self._sinks:
                self._sinks.append(sink)

    def detach(self, sink: Callable[[str, LogLevel], None]) -> None:
        """注销一个日志 sink。

        Args:
            sink: 之前注册过的回调。
        """
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def publish(self, text: str, level: LogLevel) -> None:
        """向所有 sink 广播一条日志（sink 异常被吞掉，不影响主流程）。

        Args:
            text: 已格式化的日志行。
            level: 级别。
        """
        with self._lock:
            sinks = list(self._sinks)
            self._recent.append((text, level))
            if len(self._recent) > self._recent_limit:
                del self._recent[: len(self._recent) - self._recent_limit]

        for sink in sinks:
            try:
                sink(text, level)
            except Exception:  # noqa: BLE001 - sink 故障不得影响日志主流程
                continue

    def recent(self, min_level: LogLevel = LogLevel.DEBUG) -> list[tuple[str, LogLevel]]:
        """取最近的日志行（供 UI 首次渲染回填）。

        Args:
            min_level: 最低级别过滤。

        Returns:
            ``[(text, level), ...]`` 列表。
        """
        threshold = _LEVEL_ORDER[min_level.value]
        with self._lock:
            return [
                (text, level)
                for text, level in self._recent
                if _LEVEL_ORDER[level.value] >= threshold
            ]

    def clear_recent(self) -> None:
        """清空最近日志缓冲。"""
        with self._lock:
            self._recent.clear()


#: 进程级单例日志总线
_log_bus: LogBus = LogBus()

#: 进程级根 logger 名称
_ROOT_NAME = "customs_checker"


class PhaseLogger(logging.LoggerAdapter):
    """带「阶段」前缀的 logger 适配器。

    用法::

        log = get_logger(Phase.PHASE1)
        log.info("探查 Excel 结构 → 变体 D，要素表=Sheet2")
        # 文件/信号输出： 12:00:01 INFO   Phase1 探查 Excel 结构 → 变体 D，要素表=Sheet2
    """

    def __init__(self, logger: logging.Logger, phase: Phase | str) -> None:
        phase_label = phase.value if isinstance(phase, Phase) else str(phase)
        super().__init__(logger, {"phase": phase_label})

    def process(
        self, msg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """给消息加上 ``[阶段]`` 前缀。"""
        phase = self.extra.get("phase", "") if self.extra else ""
        prefix = f"[{phase}] " if phase else ""
        return f"{prefix}{msg}", kwargs


def get_log_bus() -> LogBus:
    """返回进程级日志总线单例。"""
    return _log_bus


def set_log_bus(bus: LogBus) -> None:
    """替换进程级日志总线（主要供测试注入）。

    Args:
        bus: 新的 :class:`LogBus`。
    """
    global _log_bus
    _log_bus = bus


def attach_signal_sink(sink: Callable[[str, LogLevel], None]) -> None:
    """注册 UI 侧日志 sink（Qt 信号 emit 的包装）。

    Args:
        sink: ``(text, level) -> None`` 回调。
    """
    _log_bus.attach(sink)


def detach_signal_sink(sink: Callable[[str, LogLevel], None]) -> None:
    """注销 UI 侧日志 sink。

    Args:
        sink: 之前注册的回调。
    """
    _log_bus.detach(sink)


def setup_logging(
    log_file: str | Path | None = None,
    *,
    console: bool = True,
    level: LogLevel = LogLevel.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    bus: LogBus | None = None,
) -> logging.Logger:
    """初始化统一日志系统（幂等：重复调用会先清理旧 handler）。

    Args:
        log_file: 滚动日志文件路径；``None`` 表示不落文件（仅信号/控制台）。
        console: 是否同时输出到控制台（开发/CLI 场景）。
        level: 根级别（默认 DEBUG，文件记全量）。
        max_bytes: 单文件滚动阈值（默认 5 MiB）。
        backup_count: 保留的历史文件数。
        bus: 自定义 log bus；缺省用进程级单例。

    Returns:
        根 :class:`logging.Logger`（名为 ``customs_checker``）。
    """
    active_bus = bus if bus is not None else _log_bus

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # 幂等：清理旧 handler
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    if log_file is not None:
        file_path = normalize_path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(file_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level.py_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level.py_level)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    signal_handler = _SignalBridge(active_bus)
    signal_handler.setLevel(logging.DEBUG)
    signal_handler.setFormatter(formatter)
    root.addHandler(signal_handler)

    return root


def get_logger(phase: Phase | str = Phase.APP) -> PhaseLogger:
    """获取带阶段前缀的 logger。

    Args:
        phase: :class:`Phase` 枚举或任意阶段字符串。

    Returns:
        :class:`PhaseLogger` 实例。
    """
    base = logging.getLogger(_ROOT_NAME)
    return PhaseLogger(base, phase)
