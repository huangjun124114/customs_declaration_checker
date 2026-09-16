"""P2-1 / P2-3 回归锁：两个票号正则**语义不同、不能合并**。

批次 1 引入了 ``app.path_policy.TICKET_SEGMENT_PATTERN``（严格：目录末段整段 == 票号，
要求纯数字结尾），它与既有的 ``core.constants.TICKET_NO_PATTERN``（宽松：从 Excel 标题
行**提取**票号，尾部允许 ``[A-Za-z0-9\\-]*``）**职责不同**。

若两者被"顺手合并"，宽松正则会把以下形态误当票号：

  * ``2660310M`` —— 订单号（以字母结尾）；
  * ``N011901-009350-001`` —— 料号（含连字符）；

即 P2-2 记录的"误跑错票"风险来源。本文件用断言**固定二者的差异语义**，防止日后被合并。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.path_policy import TICKET_SEGMENT_PATTERN, infer_ticket_no_from_path
from core.constants import TICKET_NO_PATTERN

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 典型的"看起来像票号但实为别的编号"形态
_ORDER_NO_FORM = "2660310M"
_PART_NO_FORM = "N011901-009350-001"


@pytest.mark.parametrize("token", [_ORDER_NO_FORM, _PART_NO_FORM])
def test_segment_pattern_rejects_non_ticket_forms(token: str) -> None:
    """严格正则（目录末段）拒绝订单号 / 料号形态。"""
    assert re.fullmatch(TICKET_SEGMENT_PATTERN, token) is None


@pytest.mark.parametrize("token", [_ORDER_NO_FORM, _PART_NO_FORM])
def test_infer_ticket_rejects_non_ticket_forms(token: str) -> None:
    """推断函数对订单号 / 料号形态返回 ``None``（保守，不误判）。"""
    assert infer_ticket_no_from_path(token) is None


@pytest.mark.parametrize("token", [_ORDER_NO_FORM, _PART_NO_FORM])
def test_loose_pattern_accepts_non_ticket_forms(token: str) -> None:
    """宽松正则（Excel 标题行提取）**会**接受这两类形态 —— 差异的根源。"""
    match = re.search(TICKET_NO_PATTERN, f"出货通知书号:{token}")
    assert match is not None
    assert match.group("ticket") == token


def test_loose_pattern_accepts_suffixed_ticket() -> None:
    """宽松正则吞掉标题行后缀（如 ``SA26090215-01``）；严格正则对此会拒绝。"""
    match = re.search(TICKET_NO_PATTERN, "出货通知书号：SA26090215-01")
    assert match is not None
    assert match.group("ticket") == "SA26090215-01"
    assert re.fullmatch(TICKET_SEGMENT_PATTERN, "SA26090215-01") is None


def test_segment_pattern_accepts_pure_numeric_directory() -> None:
    """P2-2 风险的直接来源：纯数字日期目录名被当票号 → 故需「改票号」纠正入口。"""
    assert re.fullmatch(TICKET_SEGMENT_PATTERN, "20260902") is not None
    assert infer_ticket_no_from_path(r"D:\图片\2026年报关要素图片\张三\20260902") == "20260902"


def test_two_patterns_are_intentionally_different_from_source() -> None:
    """源码注释固定"为何不能合并"（防日后无意识合并）。"""
    source = (PROJECT_ROOT / "app" / "path_policy.py").read_text(encoding="utf-8")
    assert "TICKET_NO_PATTERN" in source
    assert "不能合并" in source
