"""``core.excel_probe`` 回归测试（T02 验收要点 ①② + 票号提取 + 表头定位 + 只读红线）。

覆盖：
  * 样本 Excel 探查 → ``variant=D`` / ``sheet_name=Sheet2`` / ``ticket_no=SA26090215`` /
    ``header_row=1``（1-based 第 2 行）；
  * **陷阱用例**：``Sheet1``（首行含"出货通知书号"列）不得被误判为要素表；
  * 票号提取容忍"缺书字 + 有/无冒号"（参数化自 ``mapping_cases.json``）；
  * 表头行定位"扫描前 N 行"（不可假定第 1 行）；
  * **只读红线**：跑批前后输入文件 mtime + sha256 不变（架构设计 9.4）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.excel_probe import (
    ExcelProbe,
    SheetInfo,
    detect_variant,
    extract_ticket_no,
    locate_header_row,
    pick_sheet_by_score,
)
from core.models import StructureVariant

# ── 夹具路径 ─────────────────────────────────────────────────────────
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_MAPPING_FIXTURE = _FIXTURES_DIR / "mapping_cases.json"

#: 真实样本文件名（架构设计 0.P1）
_SAMPLE_FILENAME = "申报要素-SA26090215委内瑞拉.XLSX"


def _load_fixture() -> dict:
    """加载字段映射回归集。"""
    return json.loads(_MAPPING_FIXTURE.read_text(encoding="utf-8"))


def _find_sample(sample_dir: Path) -> Path:
    """在样本目录下定位真实样本 Excel（找不到则 skip）。"""
    candidate = sample_dir / _SAMPLE_FILENAME
    if candidate.is_file():
        return candidate
    # 兜底：递归查找同名文件
    for found in sample_dir.rglob(_SAMPLE_FILENAME):
        return found
    pytest.skip(f"样本文件不存在：{_SAMPLE_FILENAME}")


def _sha256(path: Path) -> str:
    """计算文件 sha256（流式，避免大文件爆内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ══════════════════════════════════════════════════════════════════
#  ① 真实样本探查（T02 验收要点 ①）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("variant", StructureVariant.D_DUAL_SHEET),
        ("sheet_name", "Sheet2"),
        ("ticket_no", "SA26090215"),
    ],
)
def test_probe_sample_returns_expected(sample_dir: Path, attr: str, expected: object) -> None:
    """样本 Excel 探查须返回 variant=D / sheet_name=Sheet2 / ticket_no=SA26090215。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert getattr(result, attr) == expected


def test_probe_sample_header_row_is_second_row(sample_dir: Path) -> None:
    """表头行须定位到第 2 行（0-based=1），**不可假定第 1 行**（P3）。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert result.header_row == 1, f"期望表头行 0-based=1，实际 {result.header_row}"


def test_probe_sample_record_count_and_columns(sample_dir: Path) -> None:
    """要素表应解析出 18 条记录，且 P4 列映射正确（物料编号↔part_no）。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert len(result.records) == 18

    # P4：part_no 取「物料编号」列（三段式）、order_no 取「订单号」列（短码式）
    first = result.records[0]
    assert first.part_no == "N011901-007386-001"
    assert first.order_no == "2660326M"
    assert first.ticket_no == "SA26090215"
    assert first.seq_no == 1

    # 每行的 part_no 都应形如 N011901-*（三段式），order_no 形如 2660*M
    for record in result.records:
        assert record.part_no.startswith("N"), f"part_no 异常：{record.part_no}"
        assert record.order_no.endswith("M"), f"order_no 异常：{record.order_no}"


def test_probe_sample_p4_hit_rate_is_high(sample_dir: Path) -> None:
    """P4 自校正后命中率应达 100%（目录结构 18 票全部命中）。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert result.mapping.hit_rate == pytest.approx(1.0)
    assert result.mapping.swapped is False, "结构反推已正确，不应触发交换"


# ══════════════════════════════════════════════════════════════════
#  ② 陷阱用例：Sheet1 不得被误判为要素表（T02 验收要点 ②）
# ══════════════════════════════════════════════════════════════════


def test_trap_sheet1_not_selected_as_element_sheet(sample_dir: Path) -> None:
    """**陷阱用例**：Sheet1 首列含"出货通知书号"表头，但绝不能被选为要素表。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert result.sheet_name == "Sheet2"
    assert result.sheet_name != "Sheet1"


def test_trap_sheet_name_never_in_result(sample_dir: Path) -> None:
    """结果中不得出现 Sheet1 被当作要素表的任何迹象（records 均来自 Sheet2）。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    # 所有记录必须来自「申报要素」语义的数据（有 raw_element_text）
    assert all(record.raw_element_text for record in result.records)


def test_pick_sheet_by_score_ignores_trap_sheet() -> None:
    """打分矩阵：陷阱 Sheet（含票号列但无申报要素）得分为 0，不会被选中。"""
    trap = SheetInfo(name="Sheet1", header_row_index=0, score=0)
    element = SheetInfo(name="Sheet2", header_row_index=1, score=12)
    picked = pick_sheet_by_score([trap, element], min_score=1)
    assert picked is not None
    assert picked.name == "Sheet2"


def test_pick_sheet_by_score_returns_none_when_all_low() -> None:
    """全部低于门槛时返回 None（触发 ExcelStructureError）。"""
    trap = SheetInfo(name="Sheet1", score=0)
    assert pick_sheet_by_score([trap], min_score=1) is None


# ══════════════════════════════════════════════════════════════════
#  变体优先级回归（架构师 v1.2 裁决：D > B > C > E > A）
#  —— B/C（标题行形态）与 E（缺订单号列）正交；E 是"降级信号"而非结构类别，
#     不得因缺 order 列而丢弃已识别的标题行（含票号来源）。
# ══════════════════════════════════════════════════════════════════


def test_detect_variant_title_row_with_colon_wins_over_missing_order_col() -> None:
    """用例①：**有**标题行（带冒号）+ **无** order 列 → 判 **B**（不判 E）。"""
    sheet = SheetInfo(name="Sheet2", title_row_index=0, header_row_index=1, score=12)
    variant = detect_variant(
        sheet_count=1,
        element_sheet=sheet,
        has_order_col=False,
        ticket_has_colon=True,
    )
    assert variant is StructureVariant.B_DOC_COLON


def test_detect_variant_title_row_without_colon_wins_over_missing_order_col() -> None:
    """用例②：**有**标题行（无冒号）+ **无** order 列 → 判 **C**（不判 E）。"""
    sheet = SheetInfo(name="Sheet2", title_row_index=0, header_row_index=1, score=12)
    variant = detect_variant(
        sheet_count=1,
        element_sheet=sheet,
        has_order_col=False,
        ticket_has_colon=False,
    )
    assert variant is StructureVariant.C_DOC_NOCOLON


def test_detect_variant_no_title_row_and_missing_order_col_is_e() -> None:
    """用例③：**无**标题行 + **无** order 列 → 判 **E**（E 作为结构类别的唯一场景）。"""
    sheet = SheetInfo(name="Sheet2", title_row_index=-1, header_row_index=1, score=12)
    variant = detect_variant(
        sheet_count=1,
        element_sheet=sheet,
        has_order_col=False,
        ticket_has_colon=False,
    )
    assert variant is StructureVariant.E_NO_ORDER_COL


def test_detect_variant_priority_table_ordering() -> None:
    """优先级顺序断言：D > B/C > E > A（逐维验证，正交维度不互相吞并）。"""
    titled = SheetInfo(name="S", title_row_index=0, header_row_index=1, score=12)
    untitled = SheetInfo(name="S", title_row_index=-1, header_row_index=1, score=12)

    # D 居首：即便有标题行，多 Sheet 陷阱仍判 D
    assert (
        detect_variant(
            sheet_count=2,
            element_sheet=titled,
            has_order_col=True,
            ticket_has_colon=True,
            multi_sheet_trap=True,
        )
        is StructureVariant.D_DUAL_SHEET
    )
    # B/C 优先于 E：有标题行 + 缺 order → B/C
    assert (
        detect_variant(
            sheet_count=1, element_sheet=titled, has_order_col=False, ticket_has_colon=True
        )
        is StructureVariant.B_DOC_COLON
    )
    # E 优先于 A：单 Sheet 首行即表头 + 缺 order → E（非 A）
    single_first_header = SheetInfo(name="S", title_row_index=-1, header_row_index=0, score=12)
    assert (
        detect_variant(
            sheet_count=1,
            element_sheet=single_first_header,
            has_order_col=False,
            ticket_has_colon=False,
        )
        is StructureVariant.E_NO_ORDER_COL
    )
    # A：单 Sheet 首行即表头 + 有 order 列
    assert (
        detect_variant(
            sheet_count=1,
            element_sheet=single_first_header,
            has_order_col=True,
            ticket_has_colon=False,
        )
        is StructureVariant.A_OLD
    )
    # 无标题行 + 有 order 列 + 表头非首行 → 兜底 C
    assert (
        detect_variant(
            sheet_count=1, element_sheet=untitled, has_order_col=True, ticket_has_colon=False
        )
        is StructureVariant.C_DOC_NOCOLON
    )


def test_detect_variant_returns_unknown_for_none_sheet() -> None:
    """element_sheet 为 None → UNKNOWN（最高优先级短路）。"""
    assert (
        detect_variant(
            sheet_count=1,
            element_sheet=None,
            has_order_col=True,
            ticket_has_colon=True,
        )
        is StructureVariant.UNKNOWN
    )


def test_real_sample_still_variant_d(sample_dir: Path) -> None:
    """用例④（**真实样本回归锚点**）：SA26090215 仍判 ``D_DUAL_SHEET``。

    这是"依据点 D 仍居首位"的一致性保证：优先级重排不得影响实测样本。
    """
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert result.variant is StructureVariant.D_DUAL_SHEET
    assert result.sheet_name == "Sheet2"
    assert result.ticket_no == "SA26090215"


def test_synthetic_title_row_missing_order_col_triggers_degradation_note(
    tmp_path: Path,
) -> None:
    """合成用例：判 B/C 且缺 order 列 → notes 必须**显式**出现降级提示（不静默）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "要素表"
    sheet.append(["出货通知书号:SA26091234", None, None, None])
    # 注意：**故意不含**「订单号」列
    sheet.append(["序号", "物料编号", "名称", "申报要素"])
    sheet.append([1, "N011901-000001-001", "线材", "品牌:无|型号:无"])
    path = tmp_path / "无订单号列.xlsx"
    workbook.save(path)

    result = ExcelProbe().probe(path)
    assert result.variant in (StructureVariant.B_DOC_COLON, StructureVariant.C_DOC_NOCOLON)
    assert any("无订单号列" in note and "降级链" in note for note in result.notes), result.notes


# ══════════════════════════════════════════════════════════════════
#  票号提取（P2：缺"书"字 + 有/无冒号，参数化）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (case["texts"], case["expect"]) for case in _load_fixture()["ticket_cases"]
    ],
    ids=[case["name"] for case in _load_fixture()["ticket_cases"]],
)
def test_extract_ticket_no(texts: list[str], expected: str) -> None:
    """票号提取须容忍「出货通知书号 / 出货通知号」+ 有/无冒号（含全角）。"""
    assert extract_ticket_no(texts) == expected


def test_extract_ticket_no_from_merged_title_cell() -> None:
    """实测 P2 原文：单单元格 ``出货通知号SA26090215``（缺书字、无冒号）。"""
    assert extract_ticket_no(["出货通知号SA26090215", None, None]) == "SA26090215"


# ══════════════════════════════════════════════════════════════════
#  表头行定位（扫描前 N 行，不可假定第 1 行）
# ══════════════════════════════════════════════════════════════════


def test_locate_header_row_skips_title_row() -> None:
    """表头在第 2 行（0-based=1），标题行在第 1 行。"""
    rows = [
        ["出货通知号SA26090215", None, None, None],
        ["序号", "物料编号", "订单号", "申报要素"],
        ["1", "N011901-007386-001", "2660326M", "品牌:baori"],
    ]
    assert locate_header_row(rows) == 1


def test_locate_header_row_first_row() -> None:
    """变体 A：表头在第 1 行（0-based=0）。"""
    rows = [
        ["序号", "物料编号", "订单号", "申报要素"],
        ["1", "N011901-007386-001", "2660326M", "品牌:baori"],
    ]
    assert locate_header_row(rows) == 0


def test_locate_header_row_none_found() -> None:
    """无表头特征行 → 返回 -1。"""
    rows = [["a", "b", "c"], ["1", "2", "3"]]
    assert locate_header_row(rows) == -1


def test_locate_header_row_does_not_assume_row_one() -> None:
    """表头出现在第 4 行时仍能定位（证明"扫描前 N 行"而非"假定第 1 行"）。"""
    rows = [
        ["报表说明", None, None, None],
        [None, None, None, None],
        ["导出时间", "2026-09-16", None, None],
        ["序号", "物料编号", "订单号", "申报要素"],
    ]
    assert locate_header_row(rows) == 3


# ══════════════════════════════════════════════════════════════════
#  只读红线（架构设计 9.4 / FR-011）
# ══════════════════════════════════════════════════════════════════


def test_probe_does_not_modify_input(sample_dir: Path) -> None:
    """探查前后输入文件 mtime + sha256 必须不变（只读红线硬断言）。"""
    sample = _find_sample(sample_dir)
    mtime_before = sample.stat().st_mtime_ns
    hash_before = _sha256(sample)

    ExcelProbe().probe(sample)

    assert sample.stat().st_mtime_ns == mtime_before, "探测过程修改了输入文件（mtime 变化）"
    assert _sha256(sample) == hash_before, "探测过程修改了输入文件（内容变化）"


# ══════════════════════════════════════════════════════════════════
#  异常契约
# ══════════════════════════════════════════════════════════════════


def test_probe_missing_file_raises(tmp_path: Path) -> None:
    """路径不存在 → PathNotAccessibleError（致命级）。"""
    from infra.errors import PathNotAccessibleError

    with pytest.raises(PathNotAccessibleError):
        ExcelProbe().probe(tmp_path / "不存在.xlsx")


def test_probe_non_element_workbook_raises(tmp_path: Path) -> None:
    """无可识别要素表 → ExcelStructureError（致命级）。"""
    from openpyxl import Workbook

    from infra.errors import ExcelStructureError

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "乱七八糟"
    sheet.append(["aaa", "bbb", "ccc"])
    sheet.append([1, 2, 3])
    path = tmp_path / "无关.xlsx"
    workbook.save(path)

    with pytest.raises(ExcelStructureError):
        ExcelProbe().probe(path)


def test_probe_notes_mention_trap_sheet(sample_dir: Path) -> None:
    """探查说明须记录"检测到陷阱 Sheet"，便于追溯（可审计）。"""
    sample = _find_sample(sample_dir)
    result = ExcelProbe().probe(sample)
    assert any("陷阱" in note for note in result.notes), result.notes
