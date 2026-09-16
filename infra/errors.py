"""异常体系与用户友好中文文案映射（infra.errors，对应架构设计 9.5）。

分级策略（架构设计 9.5）：
  * **致命级（fatal）**：中止跑批 + UI 弹窗
      - ``PathNotAccessibleError``  路径不存在 / 无权限 / UNC 不可达
      - ``ExcelStructureError``     Sheet 无法定位 / 表头无法识别
      - ``TicketNoNotFoundError``   票号提取不到 → 要求用户手填
  * **记录级（record）**：记 WARN + 该条降级 ⚠️ + 继续（不中断整批）
      - ``ElementParseError``       申报要素解析失败
      - ``OcrFailureError``         单张 OCR 失败
      - ``OutputPathViolation``     越界写入（只读红线 / 产物分流违规）—— 本身是致命级红线，
                                    但通常发生在早于跑批的阶段，故单列一类
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "ErrorLevel",
    "CustomsCheckerError",
    "PathNotAccessibleError",
    "ExcelStructureError",
    "ElementParseError",
    "OcrFailureError",
    "OutputPathViolation",
    "TicketNoNotFoundError",
    "RuleConfigError",
    "FATAL_ERRORS",
    "RECORD_ERRORS",
    "is_fatal",
    "user_message_of",
]


class ErrorLevel(str, Enum):
    """异常分级。"""

    FATAL = "fatal"          # 致命级：中止跑批 + 弹窗
    RECORD = "record"        # 记录级：记 WARN，该条降级 ⚠️，继续
    REDLINE = "redline"      # 红线级：违反只读 / 分流铁律，一律中止


class CustomsCheckerError(Exception):
    """全部业务异常的基类。

    Attributes:
        user_message: 面向用户的**中文**文案（不含堆栈、含可操作建议）。
        level: 分级（:class:`ErrorLevel`）。
        context: 结构化上下文（路径、票号、行号等），供日志与 UI 展示。
    """

    #: 子类默认分级（可覆盖）
    default_level: ErrorLevel = ErrorLevel.FATAL
    #: 面向用户的默认中文文案模板（可含 ``{path}`` / ``{ticket}`` 等占位符）
    default_user_message: str = "发生未预期的错误，请联系实施人员协助排查。"

    def __init__(
        self,
        message: str = "",
        *,
        user_message: str | None = None,
        level: ErrorLevel | None = None,
        **context: Any,
    ) -> None:
        """构造异常。

        Args:
            message: 面向开发者的技术描述（进日志）。
            user_message: 面向用户的中文文案；缺省用类默认模板 + context 格式化。
            level: 覆盖默认分级。
            **context: 结构化上下文（如 ``path=...`` / ``ticket=...``）。
        """
        self.context: dict[str, Any] = dict(context)
        self.level: ErrorLevel = level if level is not None else self.default_level

        resolved_user = user_message
        if resolved_user is None:
            template = self.default_user_message
            try:
                resolved_user = template.format(**self.context)
            except (KeyError, IndexError):
                resolved_user = template
        self.user_message: str = resolved_user

        tech_message = message or self.user_message
        super().__init__(tech_message)


class PathNotAccessibleError(CustomsCheckerError):
    """路径不存在 / 无权限 / UNC 不可达（致命级）。"""

    default_level = ErrorLevel.FATAL
    default_user_message = (
        "无法访问路径：{path}\n"
        "可能原因：路径不存在、无读取权限，或共享盘（UNC）不可达。\n"
        "建议：确认路径拼写；确认已用有权限的域账号登录；确认网络/共享盘已连通。"
    )


class ExcelStructureError(CustomsCheckerError):
    """Sheet 无法定位 / 表头无法识别（致命级）。"""

    default_level = ErrorLevel.FATAL
    default_user_message = (
        "无法识别 Excel 结构：{path}\n"
        "可能原因：该文件不是「申报要素」表，或使用了尚未适配的结构变体。\n"
        "建议：确认选择的文件正确；将本文件反馈给实施人员补充适配规则。"
    )


class TicketNoNotFoundError(CustomsCheckerError):
    """票号提取不到 → 要求用户手填（致命级）。"""

    default_level = ErrorLevel.FATAL
    default_user_message = (
        "未能从 Excel 中自动识别「出货通知书号（票号）」。\n"
        "请在界面上手动填写票号后再开始校验。"
    )


class OutputPathViolation(CustomsCheckerError):
    """越界写入 / 只读红线 / 产物分流违规（红线级）。"""

    default_level = ErrorLevel.REDLINE
    default_user_message = (
        "检测到非法写入目标：{path}\n"
        "本工具遵循三条铁律：① 原始输入只读；② 产物分流（交付物进「成果产出」，"
        "过程文件进「过程产出」）；③ 不改动用户选定输入。\n"
        "已中止本次写入，请检查输出目录配置。"
    )


class ElementParseError(CustomsCheckerError):
    """申报要素解析失败（记录级，不中断）。"""

    default_level = ErrorLevel.RECORD
    default_user_message = (
        "第 {seq} 条记录（料号 {part_no}）的「申报要素」无法完整解析，"
        "已按「缺图内标识」处置，请人工复核。"
    )


class OcrFailureError(CustomsCheckerError):
    """单张 OCR 失败（记录级，不中断整批）。"""

    default_level = ErrorLevel.RECORD
    default_user_message = (
        "图片识别失败：{path}\n"
        "该张图片已跳过，本条记录将按证据不足处置，请人工复核。"
    )


class RuleConfigError(CustomsCheckerError):
    """规则配置文件（rules/*.yaml）加载 / 校验失败（致命级）。

    对应架构设计 12.C.3：校验失败须给**明确错误**（字段名 + 文件），不静默降级。
    """

    default_level = ErrorLevel.FATAL
    default_user_message = (
        "规则文件校验失败：{path}\n"
        "字段：{field}\n"
        "请修正该 YAML 文件后重启程序（规则在启动时一次性加载）。"
    )


#: 致命级异常集合（会中止跑批）
FATAL_ERRORS: tuple[type[CustomsCheckerError], ...] = (
    PathNotAccessibleError,
    ExcelStructureError,
    TicketNoNotFoundError,
    RuleConfigError,
    OutputPathViolation,
)

#: 记录级异常集合（不中断整批）
RECORD_ERRORS: tuple[type[CustomsCheckerError], ...] = (
    ElementParseError,
    OcrFailureError,
)


def is_fatal(exc: BaseException) -> bool:
    """判断异常是否为致命级（应中止跑批）。

    Args:
        exc: 任意异常实例。

    Returns:
        ``True`` 表示应中止跑批；``False`` 表示可降级继续。
    """
    if isinstance(exc, CustomsCheckerError):
        return exc.level in (ErrorLevel.FATAL, ErrorLevel.REDLINE)
    # 未包装的系统级异常一律按致命处理（宁可停下，不可带着未知状态继续）
    return True


def user_message_of(exc: BaseException) -> str:
    """提取面向用户的中文文案。

    Args:
        exc: 异常实例。

    Returns:
        业务异常返回 ``user_message``；其它异常返回通用兜底文案。
    """
    if isinstance(exc, CustomsCheckerError):
        return exc.user_message
    return f"发生未预期的错误：{type(exc).__name__}。请联系实施人员协助排查。"
