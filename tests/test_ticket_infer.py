"""v0.2.0 点 2（Q2）：从「图片根目录末段」推断票号。

现场图片目录约定为 ``…\\{年份}年报关要素图片\\{人员}\\{票号}\\``，末段通常即票号。
推断函数是**纯逻辑**（位于 ``app/``，无 Qt），是「先推断 → 推断不出再弹框」兜底链的第一步。
"""

from __future__ import annotations

import pytest

from app.path_policy import infer_ticket_no_from_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # 典型现场路径：末段 = 票号
        (r"\\172.20.99.220\成品科\2026年报关要素图片\张三\SA26090215", "SA26090215"),
        (r"D:\data\报关要素图片\李四\SA26090215", "SA26090215"),
        ("/mnt/data/tickets/ABC26090215", "ABC26090215"),
        # 尾随分隔符 / 前后空白 / 引号
        (r"\\srv\图片\张三\SA26090215\\", "SA26090215"),
        ("   SA26090215   ", "SA26090215"),
        ('"D:\\图片\\SA26090215"', "SA26090215"),
        # 纯末段（无分隔符）
        ("SA26090215", "SA26090215"),
        # 数字票号（0 字母 + 6–14 位数字）
        ("26090215", "26090215"),
    ],
)
def test_infers_ticket_like_segment(path: str, expected: str) -> None:
    assert infer_ticket_no_from_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "   ",
        # 末段含中文 → 非票号
        r"\\srv\成品科\2026年报关要素图片",
        # 末段是人员名
        r"\\srv\成品科\2026年报关要素图片\张三",
        # 订单号形态（以字母结尾）
        "2660310M",
        # 料号形态（含连字符）
        "N011901-009350-001",
        # 纯字母 / 数字太短
        "abc",
        "12",
    ],
)
def test_garbage_returns_none(path) -> None:
    assert infer_ticket_no_from_path(path) is None
