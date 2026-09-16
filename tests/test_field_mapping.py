"""``core.field_mapping`` 回归测试（T02 验收要点 ③：P4 用例）。

覆盖：
  * 列名字面映射（变体 A–E 表头参数化）；
  * **P4 交叉场景**：``part_no ↔ N011901-*``、``order_no ↔ 2660*`` 须正确对上；
  * 命中率探针在**错误映射**下能**自动交换**（``swapped=True`` + WARN）；
  * 表头打分矩阵：同时命中「申报要素」+「料号类列」才算要素表
    （**陷阱 Sheet1 不得通过**）；
  * 文件名解析 ``{A}&{B}&{C}``（含序号跳号、非标文件名容错）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.field_mapping import (
    FieldMappingBuilder,
    ProbeSample,
    build_mapping,
    evaluate_mapping,
    guess_mapping_by_literal,
    normalize_header,
    parse_image_filename,
    score_header_row,
)

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_MAPPING_FIXTURE = _FIXTURES_DIR / "mapping_cases.json"


def _fixture() -> dict:
    """加载映射回归集。"""
    return json.loads(_MAPPING_FIXTURE.read_text(encoding="utf-8"))


def _samples_from_case(case: dict) -> list[ProbeSample]:
    """把用例中的 rows + image_tokens 组装成探针样本。"""
    samples: list[ProbeSample] = []
    for row, token in zip(case["rows"], case["image_tokens"], strict=False):
        samples.append(
            ProbeSample(
                row=list(row),
                expect_order_token=token["order"],
                expect_part_token=token["part"],
            )
        )
    return samples


# ══════════════════════════════════════════════════════════════════
#  表头归一化与打分矩阵（陷阱 Sheet 必须得 0 分）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("申报要素", "申报要素"),
        ("申报要素 ", "申报要素"),
        (" 申报要素", "申报要素"),
        ("申报要素：", "申报要素"),
        ("申报要素:", "申报要素"),
        ("实物照片", "实物照片"),
        (None, ""),
        (123, "123"),
    ],
)
def test_normalize_header(raw: object, expected: str) -> None:
    """表头归一化：剥离不可见字符/冒号/空白 + 大小写折叠。"""
    assert normalize_header(raw) == expected


@pytest.mark.parametrize(
    "case",
    _fixture()["header_cases"],
    ids=[case["name"] for case in _fixture()["header_cases"]],
)
def test_score_header_row_matrix(case: dict) -> None:
    """表头打分矩阵：要素表须同时命中「申报要素」+「料号类列」。"""
    score, candidates = score_header_row(case["headers"])
    if case.get("expect_score_zero"):
        assert score == 0, f"陷阱表头不应得分，实际 {score}"
    elif case.get("expect_matches_element_and_part"):
        assert score > 0, f"要素表表头应得分：{case['name']}"
        assert any(c.field_name == "element_text" for c in candidates)
        if "expect_part_col_by_name" in case:
            part_candidates = [c for c in candidates if c.field_name == "part_no"]
            assert part_candidates, "应命中料号类列"


def test_trap_sheet1_header_scores_zero() -> None:
    """**陷阱用例**：Sheet1 首行含"出货通知书号"，但无申报要素列 → 得 0 分。"""
    trap_headers = [
        "选择", "订舱号", "出货通知书号", "柜号", "柜型",
        "封条号", "TARE", "车牌", "运输公司", "订单号",
    ]
    score, _ = score_header_row(trap_headers)
    assert score == 0


def test_score_header_row_element_col_index() -> None:
    """变体 D 表头：申报要素列索引 = 7（末列）。"""
    score, candidates = score_header_row(
        ["序号", "物料编号", "订单号", "名称", "数量", "重量", "", "申报要素"]
    )
    assert score > 0
    element = [c for c in candidates if c.field_name == "element_text"]
    assert len(element) == 1
    assert element[0].col_index == 7


# ══════════════════════════════════════════════════════════════════
#  通道 1：列名字面映射
# ══════════════════════════════════════════════════════════════════


def test_literal_mapping_variant_d() -> None:
    """变体 D 表头 → 字面映射各字段列索引正确。"""
    col_map = guess_mapping_by_literal(
        ["序号", "物料编号", "订单号", "名称", "数量", "重量", "", "申报要素"]
    )
    assert col_map["seq_no"] == 0
    assert col_map["part_no"] == 1
    assert col_map["order_no"] == 2
    assert col_map["product_name"] == 3
    assert col_map["element_text"] == 7


def test_literal_mapping_variant_e_no_order() -> None:
    """变体 E：无订单号列 → order_no 不出现。"""
    col_map = guess_mapping_by_literal(["序号", "成品料号", "名称", "申报要素"])
    assert col_map["part_no"] == 1
    assert "order_no" not in col_map


# ══════════════════════════════════════════════════════════════════
#  ③ P4 用例（T02 验收要点 ③）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "case",
    _fixture()["p4_cases"],
    ids=[case["name"] for case in _fixture()["p4_cases"]],
)
def test_p4_cases(case: dict) -> None:
    """P4 交叉场景参数化回归（映射错误 → 命中率 0；交换后 → 100%）。"""
    samples = _samples_from_case(case)
    name = case["name"]

    if name == "p4_literal_mapping_mismatch":
        result = evaluate_mapping(case["literal_col_map"], samples)
        assert result.hit_rate == pytest.approx(case["expect_literal_hit_rate"])

    elif name == "p4_structure_inversion_correct":
        result = evaluate_mapping(case["struct_col_map"], samples)
        assert result.hit_rate == pytest.approx(case["expect_hit_rate"])

    elif name == "p4_probe_auto_swap":
        # 表头列名与实际数据语义相反：字面映射必然失配
        mapping = build_mapping(
            headers=case["headers"],
            header_row=0,
            ticket_no="SA26090215",
            probe_samples=samples,
            hit_rate_threshold=0.30,
        )
        assert mapping.swapped is case["expect_swapped"]
        assert mapping.hit_rate == pytest.approx(case["expect_final_hit_rate"])
        # 交换后 part_no 应指向"N011901-*"列、order_no 指向"2660326M"列
        part_col = mapping.resolve("part_no")
        order_col = mapping.resolve("order_no")
        assert case["rows"][0][part_col].startswith("N011901")
        assert case["rows"][0][order_col].endswith("M")


def test_p4_auto_swap_sets_swapped_flag() -> None:
    """命中率探针在错误映射下**自动交换**，置 ``swapped=True``，命中率提升。"""
    headers = ["物料编号", "订单号", "申报要素"]
    rows = [
        ["2660326M", "N011901-007386-001", "品牌:baori"],
        ["2660328M", "N011901-007957-001", "品牌:无"],
        ["2660310M", "N011901-009350-001", "无品牌"],
    ]
    tokens = [
        ("2660326M", "N011901-007386-001"),
        ("2660328M", "N011901-007957-001"),
        ("2660310M", "N011901-009350-001"),
    ]
    samples = [
        ProbeSample(row=list(row), expect_order_token=order, expect_part_token=part)
        for row, (order, part) in zip(rows, tokens, strict=False)
    ]

    # 字面映射：part_no←列0（2660326M）、order_no←列1（N011901-*）→ 语义相反
    literal = guess_mapping_by_literal(headers)
    literal_result = evaluate_mapping(literal, samples)
    assert literal_result.hit_rate == pytest.approx(0.0), "字面映射应全失配（P4）"

    mapping = build_mapping(
        headers=headers, header_row=0, probe_samples=samples, hit_rate_threshold=0.30
    )
    assert mapping.swapped is True
    assert mapping.hit_rate == pytest.approx(1.0)


def test_p4_correct_mapping_no_unnecessary_swap() -> None:
    """映射已正确（命中率高）时**不得**触发交换（避免误交换）。"""
    headers = ["序号", "物料编号", "订单号", "申报要素"]
    rows = [
        ["1", "N011901-007386-001", "2660326M", "品牌:baori"],
        ["2", "N011901-007957-001", "2660328M", "品牌:无"],
    ]
    tokens = [("2660326M", "N011901-007386-001"), ("2660328M", "N011901-007957-001")]
    samples = [
        ProbeSample(row=list(r), expect_order_token=o, expect_part_token=p)
        for r, (o, p) in zip(rows, tokens, strict=False)
    ]
    mapping = build_mapping(
        headers=headers, header_row=0, probe_samples=samples, hit_rate_threshold=0.30
    )
    assert mapping.swapped is False
    assert mapping.hit_rate == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════════
#  通道 2：结构反推
# ══════════════════════════════════════════════════════════════════


def test_structure_channel_overrides_literal() -> None:
    """通道 2 以目录结构为准，纠正列名语义（P4）。"""
    headers = ["物料编号", "订单号", "申报要素"]
    literal = guess_mapping_by_literal(headers)
    assert literal["part_no"] == 0
    assert literal["order_no"] == 1

    struct_samples = [
        ProbeSample(row=["2660326M", "N011901-007386-001", "品牌:baori"],
                    expect_order_token="2660326M",
                    expect_part_token="N011901-007386-001"),
    ]
    mapping = build_mapping(
        headers=headers, header_row=0, struct_samples=struct_samples
    )
    # 结构反推：order_no → 列0、part_no → 列1
    assert mapping.resolve("order_no") == 0, mapping.notes
    assert mapping.resolve("part_no") == 1, mapping.notes
    assert mapping.channel in ("structure", "probe")


# ══════════════════════════════════════════════════════════════════
#  文件名解析（SOP 3.2 + 架构设计 6.4）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "case",
    _fixture()["filename_cases"],
    ids=[case["name"] for case in _fixture()["filename_cases"]],
)
def test_parse_image_filename(case: dict) -> None:
    """文件名 ``{A}&{B}&{C}`` 解析：A=订单段、B=料号段、C=序号（会跳号）。"""
    a_token, b_token, c_token = parse_image_filename(case["filename"])
    assert a_token == case["expect_a"]
    assert b_token == case["expect_b"]
    assert c_token == case["expect_c"]


def test_parse_image_filename_with_path() -> None:
    """含目录路径时只取文件名段。"""
    a_token, b_token, c_token = parse_image_filename(
        r"SA26090215\2660326M\2660326M&N011901-007386-001&001.jpg"
    )
    assert (a_token, b_token, c_token) == ("2660326M", "N011901-007386-001", "001")


def test_parse_image_filename_never_raises() -> None:
    """非标文件名不得抛异常，返回空三元组。"""
    assert parse_image_filename("") == ("", "", "")
    assert parse_image_filename("随便一个名字") == ("", "", "")
    assert parse_image_filename("a&b") == ("", "", "")


# ══════════════════════════════════════════════════════════════════
#  FieldMappingBuilder 门面 + FieldMapping.resolve
# ══════════════════════════════════════════════════════════════════


def test_builder_produces_field_mapping() -> None:
    """``FieldMappingBuilder`` 门面产出可用的 :class:`FieldMapping`。"""
    builder = FieldMappingBuilder(ticket_no="SA26090215")
    builder.set_headers(["序号", "物料编号", "订单号", "申报要素"], header_row=1)
    mapping = builder.build()

    assert mapping.ticket_no == "SA26090215"
    assert mapping.header_row == 1
    assert mapping.resolve("part_no") == 1
    assert mapping.resolve("element_text") == 3
    assert mapping.resolve("不存在字段") == -1


def test_field_mapping_to_dict_roundtrip() -> None:
    """``FieldMapping.to_dict()`` 结构完整。"""
    builder = FieldMappingBuilder(ticket_no="X")
    builder.set_headers(["序号", "物料编号", "订单号", "申报要素"], header_row=0)
    data = builder.build().to_dict()
    assert data["ticket_no"] == "X"
    assert data["col_map"]["part_no"] == 1
    assert "channel" in data and "swapped" in data
