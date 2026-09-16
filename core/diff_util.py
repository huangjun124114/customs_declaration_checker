"""逐字符差异工具（core.diff_util，对应架构设计第 4 节 + SOP 3.4 规则 5）。

职责：
  * 给定「申报值」与「图片识别值」，产出**逐字符差异点列表**（``DifferenceDetail.char_diffs``）。
  * SOP 3.4 规则 5 明确要求：**型号不一致时必须列出不同点**。本模块是把该口径
    落成可测试实现的最小单元。

**口径声明**：本模块**不新增、不改写、不弱化**任何判定口径。它只是"把差异写成人类
可读字符对"的展示型工具——"是否判异常"的决策在 :mod:`core.judge_engine`。

设计要点：
  * 纯函数、无副作用、可重复调用结果一致（与 ``JudgeEngine`` 同级契约）。
  * 使用 :mod:`difflib.SequenceMatcher` 对齐公共片段，只对 ``replace`` / ``delete`` /
    ``insert`` 三类操作产出差异描述。
  * 字符差异描述采用 ``『申报X』→『图片Y』`` 的人类可读格式，逐位定位。
"""

from __future__ import annotations

import difflib
from typing import Any

__all__ = [
    "char_diff",
    "difference_ratio",
    "describe_differences",
]


#: 差异描述的**位置基准**说明（进日志 / 差异备注时的可读性保障）。
_INDEX_BASE_NOTE = "位置从 0 起算"


def char_diff(declared: str, detected: str) -> list[str]:
    """产出两个字符串的**逐字符差异点**列表。

    以 :class:`difflib.SequenceMatcher` 的 opcode 对齐公共片段，对每个非 ``equal``
    片段产出可读描述：

      * ``replace`` —— ``『申报a』→『图片b』（第N位起）``
      * ``delete``  —— ``申报多出『a』（第N位起）``
      * ``insert``  —— ``图片多出『b』（第N位起）``

    Args:
        declared: 申报值（品牌 / 型号）。
        detected: 图片识别值。

    Returns:
        差异描述字符串列表；两者完全一致时返回空列表 ``[]``。

    Examples:
        >>> char_diff("A7A01G", "A7A02G")
        ["『申报1』→『图片2』（第4位起）"]
        >>> char_diff("SKYWORTH", "SKYHORTH")
        ["『申报W』→『图片H』（第4位起）"]
    """
    left = "" if declared is None else str(declared)
    right = "" if detected is None else str(detected)

    if left == right:
        return []

    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    diffs: list[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        declared_segment = left[i1:i2]
        detected_segment = right[j1:j2]

        if tag == "replace":
            diffs.append(
                f"『申报{declared_segment}』→『图片{detected_segment}』（第{i1}位起）"
            )
        elif tag == "delete":
            diffs.append(f"申报多出『{declared_segment}』（第{i1}位起）")
        elif tag == "insert":
            diffs.append(f"图片多出『{detected_segment}』（第{j1}位起）")
        else:  # pragma: no cover - difflib 只产出上述四类
            diffs.append(
                f"『申报{declared_segment}』≠『图片{detected_segment}』（第{i1}位起）"
            )

    return diffs


def difference_ratio(left: str, right: str) -> float:
    """返回两字符串的相似度（0.0–1.0，1.0 表示完全相同）。

    用于 ``NoiseGuard`` 的模糊相似度辅助信号与日志展示；**不作为判定口径**，
    真正的疑似噪声判定在 :mod:`core.noise_guard`（基于规则表 + 编辑距离）。

    Args:
        left: 左侧字符串。
        right: 右侧字符串。

    Returns:
        相似度比值；两者均空视为 ``1.0``（等价）。
    """
    a = "" if left is None else str(left)
    b = "" if right is None else str(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return float(matcher.ratio())


def describe_differences(
    field: str,
    declared: str,
    detected: str,
    *,
    note: str = "",
) -> dict[str, Any]:
    """把差异打包为 :class:`core.models.DifferenceDetail` 的可构造字典。

    本函数只做"字段名 + 双方值 + 逐字符差异 + 备注"的组装，**不做任何判定**；
    调用方（``JudgeEngine``）负责决定是否判异常。

    Args:
        field: 字段名（``品牌`` / ``型号``）。
        declared: 申报值。
        detected: 图片识别值。
        note: 补充说明（如"外箱整机品牌，不构成本体证据"）。

    Returns:
        可直接展开传给 ``DifferenceDetail(**result)`` 的字典。
        另附带 ``"_index_base"`` 提示键（仅供日志可读性，构造时需剔除）。

    Examples:
        >>> d = describe_differences("型号", "A7A01G", "A7A02G")
        >>> d["field"], d["char_diffs"]
        ('型号', ["『申报1』→『图片2』（第4位起）"])
    """
    return {
        "field": field or "",
        "declared_value": "" if declared is None else str(declared),
        "detected_value": "" if detected is None else str(detected),
        "char_diffs": char_diff(declared, detected),
        "note": note or "",
        "_index_base": _INDEX_BASE_NOTE,
    }
