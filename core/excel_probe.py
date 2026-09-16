"""Excel 结构探查（core.excel_probe，对应架构设计 4 节类图 + 5 节链路A + 6 节 + SOP Phase1）。

**职责**（SOP Phase 1）：
  * Sheet 变体 A–E 识别（**列名打分矩阵**，不用"含出货通知书号列的 Sheet"）；
  * 要素表定位（**陷阱**：``Sheet1`` 箱明细首列含"出货通知书号"，绝不能被选为要素表）；
  * 表头行定位（**扫描前 N 行 + 列名命中打分**，不可假定第 1 行）；
  * 票号提取（**容忍"出货通知书号 / 出货通知号"缺"书"字 + 有/无冒号**）；
  * 产出 :class:`core.models.FieldMapping`（含 P4 三通道自校正）与
    ``DeclarationRecord`` 列表（申报要素解析由 :class:`~core.element_parser.ElementParser` 完成）。

**只读红线（架构设计 9.4）**：openpyxl **必须以**
``load_workbook(path, read_only=True, data_only=True)`` 打开，**绝不修改输入文件**。

**依赖层次**：``core/`` 只 import ``infra/`` 与 ``core/``，**禁止 import PySide6/PyQt**
（架构守卫测试 ``tests/test_architecture_guard.py`` 强制）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import constants as C
from core.field_mapping import (
    FieldMappingBuilder,
    ProbeSample,
    guess_mapping_by_literal,
    parse_image_filename,
    score_header_row,
)
from core.models import (
    DeclarationRecord,
    ExcelProbeResult,
    StructureVariant,
)
from infra.errors import ExcelStructureError, PathNotAccessibleError
from infra.logger import Phase, get_logger

__all__ = [
    "SheetInfo",
    "ExcelProbe",
    "probe_excel",
    "detect_variant",
    "locate_header_row",
    "extract_ticket_no",
    "pick_sheet_by_score",
]

_logger = get_logger(Phase.PHASE1)

#: 表头扫描行数上限（架构设计：扫描前 N 行 + 列名打分定位表头，不可假定第 1 行）
HEADER_SCAN_ROWS: int = 8

#: 标题行关键词（来自 constants，用于区分「标题行」与「表头列名行」）
_TITLE_KEYWORDS: tuple[str, ...] = tuple(C.TICKET_TITLE_KEYWORDS)

#: 表头关键词归一化后用于"该行是列名行"的判定
_NAME_COL_HINTS: tuple[str, ...] = tuple(
    _h.strip().upper() for _h in C.SHEET_HEADER_HINTS.get("name_col", [])
)

#: 常见数字型列名（判定表头行时作为辅助信号）
_STRONG_HEADER_KEYS: tuple[str, ...] = ("申报要素",)


# ══════════════════════════════════════════════════════════════════
#  Sheet 结构描述
# ══════════════════════════════════════════════════════════════════


@dataclass
class SheetInfo:
    """单个 Sheet 的探查信息。

    Attributes:
        name: Sheet 名。
        title_row_index: 标题行（含票号）的 0-based 行索引；无标题为 ``-1``。
        header_row_index: 表头行的 0-based 行索引；无表头为 ``-1``。
        score: 该 Sheet 作为要素表的打分（见 :func:`pick_sheet_by_score`）。
        header_cells: 表头行单元格原始值。
        ticket_no: 从标题行提取的票号。
    """

    name: str = ""
    title_row_index: int = -1
    header_row_index: int = -1
    score: int = 0
    header_cells: list[Any] = field(default_factory=list)
    ticket_no: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（调试用）。"""
        return {
            "name": self.name,
            "title_row_index": self.title_row_index,
            "header_row_index": self.header_row_index,
            "score": self.score,
            "ticket_no": self.ticket_no,
        }


# ══════════════════════════════════════════════════════════════════
#  模块级纯函数（供单测直接调用）
# ══════════════════════════════════════════════════════════════════


def _as_text(value: Any) -> str:
    """把单元格值安全转为 ``str``。"""
    return "" if value is None else str(value)


def extract_ticket_no(texts: Iterable[Any]) -> str:
    """从若干文本片段中提取票号（架构设计 0.P2 + 6.2）。

    容忍：``出货通知书号:SA26090215`` / ``出货通知号SA26090215``（**缺"书"字**）/
    ``出货通知书号 SA26090215``（**无冒号** / 有空格）。

    Args:
        texts: 文本片段序列（表格标题行单元格等）。

    Returns:
        票号字符串；提取不到返回 ``""``（由上层决定是否要求用户手填）。
    """
    pattern = re.compile(C.TICKET_NO_PATTERN)
    for text in texts:
        box = _as_text(text)
        if not box:
            continue
        match = pattern.search(box)
        if match is not None:
            ticket = match.group("ticket").strip()
            if ticket:
                return ticket
    return ""


def _looks_like_header_row(cells: Sequence[Any]) -> bool:
    """判断一行是否"像表头列名行"。

    判据（任一）：
      * 命中「申报要素」等强表头关键词；
      * 同时命中「名称/品名」类 + 至少 2 个其它列名词。

    Args:
        cells: 该行单元格值。

    Returns:
        ``True`` 表示像表头行。
    """
    total_score, _ = score_header_row(cells)
    if total_score > 0:
        return True

    # 兜底：无料号列时，用"名称/品名 + ≥2 个其他列名"判定
    from core.field_mapping import normalize_header

    norm_cells = [normalize_header(c) for c in cells]
    has_name = any(h in _NAME_COL_HINTS for h in norm_cells if h)
    hits = 0
    for hint_group in C.SHEET_HEADER_HINTS.values():
        for raw_kw in hint_group:
            kw = normalize_header(raw_kw)
            if kw and any(kw == h or (len(kw) >= 2 and kw in h) for h in norm_cells if h):
                hits += 1
                break
    return has_name and hits >= 3


def locate_header_row(rows: Sequence[Sequence[Any]]) -> int:
    """在前 N 行中定位表头行（架构设计 0.P3：**不可假定第 1 行**）。

    打分 = :func:`core.field_mapping.score_header_row` 的得分；取最高分所在行；
    平分时取**最靠前**者（表头通常靠前）。

    Args:
        rows: 行数据（每行是单元格值序列），通常为前 :data:`HEADER_SCAN_ROWS` 行。

    Returns:
        表头行的 0-based 行索引；找不到返回 ``-1``。
    """
    best_index = -1
    best_score = 0
    for idx, row in enumerate(rows):
        score, _ = score_header_row(row)
        if score <= 0:
            # 兜底：宽松判定（无料号列的变体 E）
            if _looks_like_header_row(row):
                score = 1
        if score > best_score:
            best_score = score
            best_index = idx
    return best_index


def detect_variant(
    *,
    sheet_count: int,
    element_sheet: SheetInfo | None,
    has_order_col: bool,
    ticket_has_colon: bool,
    multi_sheet_trap: bool = False,
) -> StructureVariant:
    """判定 Excel 结构变体 A–E（架构设计 4 节 StructureVariant + SOP Phase1）。

    判定优先级（**架构师 v1.2 回归裁决：``D > B > C > E > A``**）：

    ====== ====================================================
    变体   特征
    ====== ====================================================
    D      多 Sheet 且存在"陷阱 Sheet"（首列含票号列但非要素表）
    B      标题含冒号（``出货通知书号:SA…``）—— 标题行形态
    C      标题无冒号（``出货通知号SA…``，缺"书"字）—— 标题行形态
    E      **降级信号**：要素表无「订单号」列（无标题行时才作为类别）
    A      单 Sheet 且首行即表头（老结构）
    UNKNOWN 无法判定
    ====== ====================================================

    **为什么 B/C 优先于 E**：B/C（标题行形态）与 E（缺订单号列）是**正交的两个维度**，
    E 本质是"降级信号"而非"结构类别"。若把 E 排在 B/C 之前，会导致"已识别到的标题行
    （含票号来源）被丢弃、退化成最弱变体"。

    已确认 ``ImageResolver`` **不消费 ``variant``**（它只用 ``order_no`` / ``part_no``），
    故判 B/C 不改变下游行为；而 ``order_no`` 为空导致的全票失配风险由
    ``ImageResolver`` 降级链③④兜底，不会丢失（降级语义由
    :meth:`ExcelProbe._probe_workbook` 通过 ``notes`` 显式表达）。

    Args:
        sheet_count: 工作簿 Sheet 数。
        element_sheet: 被选中的要素表信息。
        has_order_col: 要素表是否含「订单号」列。
        ticket_has_colon: 标题票号是否带冒号。
        multi_sheet_trap: 是否存在"陷阱 Sheet"。

    Returns:
        :class:`core.models.StructureVariant`。
    """
    if element_sheet is None:
        return StructureVariant.UNKNOWN

    # D：双 Sheet 陷阱（实测样本：Sheet1 箱明细含票号列，Sheet2 才是要素表）
    if sheet_count >= 2 and multi_sheet_trap:
        return StructureVariant.D_DUAL_SHEET

    # B / C：标题在独立行（单单元格）—— 有/无冒号
    # **优先于 E**：标题行形态（含票号来源）不得因"缺订单号列"被丢弃
    if element_sheet.title_row_index >= 0:
        return (
            StructureVariant.B_DOC_COLON
            if ticket_has_colon
            else StructureVariant.C_DOC_NOCOLON
        )

    # E：要素表无订单号列（**降级信号**；仅在无标题行时作为结构类别）
    if not has_order_col:
        return StructureVariant.E_NO_ORDER_COL

    # A：单 Sheet，表头位于第 1 行
    if sheet_count == 1 and element_sheet.header_row_index == 0:
        return StructureVariant.A_OLD

    # 兜底：有独立表头行但无标题行
    return StructureVariant.C_DOC_NOCOLON


def pick_sheet_by_score(
    sheet_infos: Sequence[SheetInfo],
    min_score: int = 1,
) -> SheetInfo | None:
    """用**列名打分矩阵**选出要素表（架构设计 1.C1）。

    **关键**：要求同时命中「申报要素」+「料号类列」（由
    :func:`core.field_mapping.score_header_row` 保证）——**绝不能**用
    "含出货通知书号列的 Sheet"（会被 ``Sheet1`` 箱明细误中）。

    Args:
        sheet_infos: 各 Sheet 的探查信息。
        min_score: 最低得分门槛。

    Returns:
        得分最高的 :class:`SheetInfo`；全部低于门槛返回 ``None``。
    """
    best: SheetInfo | None = None
    for info in sheet_infos:
        if info.score < min_score:
            continue
        if best is None or info.score > best.score:
            best = info
    return best


# ══════════════════════════════════════════════════════════════════
#  主探查器
# ══════════════════════════════════════════════════════════════════


class ExcelProbe:
    """Excel 结构探查器（SOP Phase 1）。

    典型用法::

        probe = ExcelProbe(parser=element_parser)
        result = probe.probe(Path("申报要素-SA26090215委内瑞拉.XLSX"))
        # result.variant == StructureVariant.D_DUAL_SHEET
        # result.sheet_name == "Sheet2"
        # result.ticket_no == "SA26090215"
        # result.header_row == 1   (0-based；对外口径的"第 2 行")

    Attributes:
        parser: 申报要素解析器（可为 ``None``，则 ``records`` 的 brand/model 留空）。
        header_scan_rows: 表头扫描行数上限。
        hit_rate_threshold: 命中率探针阈值（透传 ``FieldMappingBuilder``）。
    """

    def __init__(
        self,
        parser: Any | None = None,
        header_scan_rows: int = HEADER_SCAN_ROWS,
        hit_rate_threshold: float = 0.30,
    ) -> None:
        """构造探查器。

        Args:
            parser: :class:`core.element_parser.ElementParser` 实例（延迟使用，
                避免循环 import：本模块只按鸭子类型调用 ``parse``）。
            header_scan_rows: 表头扫描行数上限。
            hit_rate_threshold: 命中率探针阈值。
        """
        self.parser = parser
        self.header_scan_rows: int = header_scan_rows
        self.hit_rate_threshold: float = hit_rate_threshold

    # ─────────────────── 公开接口 ───────────────────

    def probe(self, path: str | Path) -> ExcelProbeResult:
        """探查 Excel 结构并返回探查结果（架构设计 4 节接口契约）。

        **只读打开**：``load_workbook(path, read_only=True, data_only=True)``。

        Args:
            path: Excel 文件路径（可为本地或 UNC）。

        Returns:
            :class:`core.models.ExcelProbeResult`。

        Raises:
            PathNotAccessibleError: 路径不存在 / 不可读。
            ExcelStructureError: 无法定位要素表或表头 / 票号缺失。
        """
        excel_path = Path(path)
        if not excel_path.exists():
            raise PathNotAccessibleError(
                f"Excel 文件不存在：{excel_path}", path=str(excel_path)
            )

        try:
            # 只读红线（架构设计 9.4）：绝不修改输入文件
            from openpyxl import load_workbook

            workbook = load_workbook(str(excel_path), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - 统一转业务异常
            raise PathNotAccessibleError(
                f"Excel 文件无法打开（可能被占用或权限不足）：{exc}",
                path=str(excel_path),
            ) from exc

        try:
            return self._probe_workbook(workbook, excel_path)
        finally:
            workbook.close()

    # ─────────────────── 内部实现（类图方法） ───────────────────

    def _probe_workbook(self, workbook: Any, excel_path: Path) -> ExcelProbeResult:
        """在已打开的 workbook 上执行探查（便于单测注入假 workbook）。

        Args:
            workbook: openpyxl workbook（或兼容对象）。
            excel_path: 原始路径（错误信息用）。

        Returns:
            :class:`core.models.ExcelProbeResult`。
        """
        sheet_names: list[str] = list(workbook.sheetnames)
        notes: list[str] = []

        # ① 逐 Sheet 打分 + 定位表头 + 提取标题
        infos: list[SheetInfo] = []
        for name in sheet_names:
            info = self._scan_sheet(workbook[name], name)
            infos.append(info)
            notes.append(
                f"Sheet「{name}」：打分={info.score}，表头行={info.header_row_index}，"
                f"标题行={info.title_row_index}，票号={info.ticket_no or '（无）'}"
            )

        # ② 用列名打分矩阵选要素表（陷阱：绝不能被 Sheet1 误中）
        element_sheet = pick_sheet_by_score(infos, min_score=1)
        if element_sheet is None:
            raise ExcelStructureError(
                f"无法定位「申报要素」要素表（未发现同时含申报要素列与料号类列的表头）："
                f"{excel_path}",
                path=str(excel_path),
            )

        # ③ 陷阱判定：存在"含票号列但非要素表"的 Sheet → D 变体
        multi_sheet_trap = any(
            info.name != element_sheet.name
            and info.title_row_index < 0
            and self._has_ticket_column(workbook[info.name])
            for info in infos
        )
        if multi_sheet_trap:
            notes.append(
                "检测到陷阱 Sheet（含票号列但非要素表），已排除，变体判为 D"
            )

        # ④ 表头行 + 列名
        header_row = element_sheet.header_row_index
        if header_row < 0:
            raise ExcelStructureError(
                f"要素表「{element_sheet.name}」未找到可识别的表头行：{excel_path}",
                path=str(excel_path),
            )
        headers = element_sheet.header_cells

        # ⑤ 票号（优先要素表标题；兜底任一 Sheet）
        ticket_no = element_sheet.ticket_no or self._extract_ticket_no_any(infos)
        ticket_has_colon = self._ticket_has_colon(workbook, element_sheet)

        # ⑥ 数据行（表头行之后，跳过全空行与重复表头行）
        data_rows = self._collect_data_rows(
            workbook[element_sheet.name], header_row + 1, headers
        )

        # ⑦ 变体判定
        literal_map = guess_mapping_by_literal(headers)
        has_order_col = "order_no" in literal_map
        variant = detect_variant(
            sheet_count=len(sheet_names),
            element_sheet=element_sheet,
            has_order_col=has_order_col,
            ticket_has_colon=ticket_has_colon,
            multi_sheet_trap=multi_sheet_trap,
        )

        # ⑦.1 显式降级提示：判 B/C（标题行形态优先）但缺订单号列 → 不静默
        if (
            variant in (StructureVariant.B_DOC_COLON, StructureVariant.C_DOC_NOCOLON)
            and not has_order_col
        ):
            notes.append(
                "变体判为 B/C（标题行形态优先），但要素表无订单号列：订单号将为空，"
                "图片匹配将走降级链兜底（子目录扫描 / 票号根目录解析）"
            )

        # ⑧ 字段映射（P4 三通道自校正）
        builder = FieldMappingBuilder(
            ticket_no=ticket_no, hit_rate_threshold=self.hit_rate_threshold
        )
        builder.set_headers(headers, header_row)
        struct_samples, probe_samples = self._build_mapping_samples(
            data_rows, ticket_no, excel_path
        )
        for sample in struct_samples:
            builder.add_struct_sample(sample)
        for sample in probe_samples:
            builder.add_probe_sample(sample)
        mapping = builder.build()
        notes.extend(mapping.notes)

        # ⑨ 组装申报记录
        records = self._build_records(
            data_rows=data_rows,
            mapping_col_map=mapping.col_map,
            ticket_no=ticket_no,
            variant=variant,
            header_row=header_row,
        )

        if not ticket_no:
            notes.append("未提取到票号（需由上层要求用户手填，对应 Q9）")

        result = ExcelProbeResult(
            variant=variant,
            sheet_name=element_sheet.name,
            header_row=header_row,
            ticket_no=ticket_no,
            mapping=mapping,
            records=records,
            notes=notes,
        )
        _logger.info(
            "探查完成 → 变体 %s，要素表=%s，表头行=%s，票号=%s，记录数=%d，"
            "命中率=%.0f%%，swapped=%s",
            variant.value,
            element_sheet.name,
            header_row,
            ticket_no or "（无）",
            len(records),
            mapping.hit_rate * 100,
            mapping.swapped,
        )
        return result

    def _scan_sheet(self, worksheet: Any, name: str) -> SheetInfo:
        """扫描单个 Sheet：表头行 + 标题行 + 票号 + 打分。

        Args:
            worksheet: openpyxl worksheet。
            name: Sheet 名。

        Returns:
            :class:`SheetInfo`。
        """
        info = SheetInfo(name=name)

        # 预取前 N 行（read_only 模式下 iter_rows 可安全多轮调用）
        head_rows = self._read_rows(worksheet, max_row=self.header_scan_rows)

        header_index = locate_header_row(head_rows)
        if header_index >= 0:
            info.header_row_index = header_index
            info.header_cells = list(head_rows[header_index])
            info.score, _ = score_header_row(info.header_cells)

        # 标题行（含票号）通常在表头行之前
        for idx, row in enumerate(head_rows[: max(header_index, 0) + 2]):
            if idx == header_index:
                continue
            cells = list(row)
            ticket = extract_ticket_no(cells)
            if ticket:
                info.title_row_index = idx
                info.ticket_no = ticket
                break

        return info

    @staticmethod
    def _read_rows(worksheet: Any, max_row: int) -> list[list[Any]]:
        """读取前 ``max_row`` 行（容错空表）。

        Args:
            worksheet: openpyxl worksheet。
            max_row: 读取行数上限。

        Returns:
            行列表（每行为单元格值列表）。
        """
        rows: list[list[Any]] = []
        try:
            for row in worksheet.iter_rows(min_row=1, max_row=max_row, values_only=True):
                rows.append(list(row))
        except Exception as exc:  # noqa: BLE001 - 单 Sheet 读取失败不应中断整体探查
            _logger.warning("Sheet 行读取异常（已跳过）：%s", exc)
        return rows

    @staticmethod
    def _has_ticket_column(worksheet: Any) -> bool:
        """判断该 Sheet 首行/全行是否含「出货通知书号」列名（陷阱 Sheet 特征）。

        Args:
            worksheet: openpyxl worksheet。

        Returns:
            ``True`` 表示存在票号列名。
        """
        title_keywords = [kw.upper() for kw in _TITLE_KEYWORDS]
        try:
            for row in worksheet.iter_rows(
                min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True
            ):
                for cell in row:
                    text = _as_text(cell).strip().upper()
                    if text and any(kw in text for kw in title_keywords):
                        return True
        except Exception:  # noqa: BLE001
            return False
        return False

    @staticmethod
    def _extract_ticket_no_any(infos: Sequence[SheetInfo]) -> str:
        """从所有 Sheet 信息中兜底提取票号。"""
        for info in infos:
            if info.ticket_no:
                return info.ticket_no
        return ""

    @staticmethod
    def _ticket_has_colon(workbook: Any, element_sheet: SheetInfo) -> bool:
        """判断要素表标题票号是否带冒号（变体 B/C 区分）。

        Args:
            workbook: openpyxl workbook。
            element_sheet: 要素表信息。

        Returns:
            ``True`` 表示带冒号（含全角 ``：``）。
        """
        if element_sheet.title_row_index < 0:
            return False
        try:
            worksheet = workbook[element_sheet.name]
            for row in worksheet.iter_rows(
                min_row=element_sheet.title_row_index + 1,
                max_row=element_sheet.title_row_index + 1,
                values_only=True,
            ):
                for cell in row:
                    text = _as_text(cell)
                    if element_sheet.ticket_no and element_sheet.ticket_no in text:
                        prefix = text.split(element_sheet.ticket_no, 1)[0]
                        return ":" in prefix or "：" in prefix
        except Exception:  # noqa: BLE001
            return False
        return False

    @staticmethod
    def _collect_data_rows(
        worksheet: Any, start_row: int, headers: Sequence[Any]
    ) -> list[tuple[int, list[Any]]]:
        """收集表头行之后的数据行（0-based 行号 + 单元格值）。

        跳过：全空行、与表头相同的重复表头行。

        Args:
            worksheet: openpyxl worksheet。
            start_row: 起始行（1-based，表头行的下一行）。
            headers: 表头单元格值（用于识别重复表头行）。

        Returns:
            ``[(row_index_0based, cells), ...]``。
        """
        from core.field_mapping import normalize_header

        header_norms = [normalize_header(h) for h in headers]
        data: list[tuple[int, list[Any]]] = []
        try:
            for row in worksheet.iter_rows(min_row=start_row, values_only=True):
                cells = list(row)
                if all(_as_text(c).strip() == "" for c in cells):
                    continue
                row_norms = [normalize_header(c) for c in cells]
                if row_norms == header_norms:
                    continue
                # 行号：iter_rows 从 start_row 开始，按序递增
                index_0based = start_row - 1 + len(data)
                data.append((index_0based, cells))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("数据行读取异常（已截断）：%s", exc)
        return data

    def _build_mapping_samples(
        self,
        data_rows: Sequence[tuple[int, list[Any]]],
        ticket_no: str,
        excel_path: Path,
    ) -> tuple[list[ProbeSample], list[ProbeSample]]:
        """构造通道 2（结构）与通道 3（命中率）样本。

        关键（P4）：本方法**主动读取共享目录结构** ``{票号}\\{X}\\{X}&{Y}&{序号}.jpg``
        以反推真实映射（架构设计 6.3 通道 2）。

        Args:
            data_rows: 数据行。
            ticket_no: 票号。
            excel_path: Excel 路径（用于定位票号目录）。

        Returns:
            ``(struct_samples, probe_samples)``。
        """
        if not data_rows:
            return [], []

        # 反推目录结构：在 Excel 同级 / 上级寻找 {票号} 目录
        dir_ab_map = self._scan_ticket_directory(ticket_no, excel_path)

        struct_samples: list[ProbeSample] = []
        probe_samples: list[ProbeSample] = []

        max_samples = min(len(data_rows), 20)
        for _idx, (_row_index, cells) in enumerate(data_rows[:max_samples]):
            row_texts = [_as_text(c).strip() for c in cells]

            # 从 G 列拼接（料号+订单号连写）中反推 A/B 段
            a_token, b_token = self._infer_tokens_from_row(row_texts)

            if not a_token and not b_token and dir_ab_map:
                # 用目录结构顺序兜底：第 k 条记录对应第 k 个 (X, Y)
                key = _idx % len(dir_ab_map)
                a_token, b_token = dir_ab_map[key]

            sample = ProbeSample(
                row=row_texts,
                expect_order_token=a_token,
                expect_part_token=b_token,
            )
            if a_token or b_token:
                struct_samples.append(sample)
            probe_samples.append(sample)

        return struct_samples, probe_samples

    @staticmethod
    def _scan_ticket_directory(
        ticket_no: str, excel_path: Path
    ) -> list[tuple[str, str]]:
        """扫描 ``{票号}`` 目录下的图片文件名，返回 ``[(A, B), ...]``（去重保序）。

        Args:
            ticket_no: 票号。
            excel_path: Excel 路径。

        Returns:
            ``[(目录首段, 文件名第二段), ...]``；无目录/无图片返回 ``[]``。
        """
        if not ticket_no:
            return []
        candidates: list[Path] = []
        # ① Excel 同级目录下的 {票号}
        candidates.append(excel_path.parent / ticket_no)
        # ② 工程/样本常见位置：原始输入/{票号}
        candidates.append(excel_path.parent / "原始输入" / ticket_no)

        ticket_dir: Path | None = None
        for candidate in candidates:
            if candidate.is_dir():
                ticket_dir = candidate
                break
        if ticket_dir is None:
            return []

        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            # 递归一层：{票号}\{X}\{X}&{Y}&{n}.jpg 与 {票号}\{X}&{Y}&{n}.jpg
            for path in sorted(ticket_dir.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp"}:
                    continue
                a_token, b_token, _c = parse_image_filename(path.name)
                if a_token and b_token:
                    pair = (a_token, b_token)
                    if pair not in seen:
                        seen.add(pair)
                        pairs.append(pair)
        except Exception as exc:  # noqa: BLE001 - 目录不可达不影响探查主流程
            _logger.warning("票号目录扫描失败（不影响映射）：%s", exc)
        return pairs

    @staticmethod
    def _infer_tokens_from_row(row_texts: Sequence[str]) -> tuple[str, str]:
        """从行内「料号+订单号连写」列反推 ``(A, B)`` 段（自校正交叉验证）。

        实测 G 列 = ``N011901-007386-0012660326M``（料号明文 + 订单号明文连写）。
        本方法不做列名假设，而是**在图样上**寻找"某列值 = 另一列值 + 第三列值"的
        三元关系，从而在只有 Excel、没有目录时也能反推 P4 语义。

        Args:
            row_texts: 该行各单元格文本（已 strip）。

        Returns:
            ``(A_token, B_token)``；反推不出返回 ``("", "")``。
        """
        cells = [c for c in row_texts if c]
        if len(cells) < 3:
            return "", ""

        # 枚举三元组 (i, j, k) 满足 cells[k] == cells[i] + cells[j]
        for i, left in enumerate(cells):
            for j, right in enumerate(cells):
                if i == j:
                    continue
                concat = left + right
                for k, whole in enumerate(cells):
                    if k in (i, j):
                        continue
                    if whole == concat:
                        # 语义映射（实测）：
                        #   图片首段 A = Excel「订单号」列值
                        #   图片第二段 B = Excel「物料编号」列值
                        # 「订单号」列值形如 2660326M（无连字符、以字母结尾）；
                        # 「物料编号」列值形如 N011901-007386-001（含连字符三段式）。
                        if "-" in right and "-" not in left:
                            return left, right
                        if "-" in left and "-" not in right:
                            return right, left
                        return left, right
        return "", ""

    def _build_records(
        self,
        data_rows: Sequence[tuple[int, list[Any]]],
        mapping_col_map: dict[str, int],
        ticket_no: str,
        variant: StructureVariant,
        header_row: int,
    ) -> list[DeclarationRecord]:
        """把数据行组装为 :class:`DeclarationRecord` 列表（含要素解析）。

        Args:
            data_rows: 数据行。
            mapping_col_map: ``{内部字段: 列索引}``。
            ticket_no: 票号。
            variant: 结构变体。
            header_row: 表头行号（0-based）。

        Returns:
            记录列表。
        """
        records: list[DeclarationRecord] = []
        for row_index, cells in data_rows:
            texts = [_as_text(c) for c in cells]
            part_no = self._cell(texts, mapping_col_map.get("part_no", -1))
            order_no = self._cell(texts, mapping_col_map.get("order_no", -1))
            product_name = self._cell(texts, mapping_col_map.get("product_name", -1))
            raw_element = self._cell(texts, mapping_col_map.get("element_text", -1))
            seq_text = self._cell(texts, mapping_col_map.get("seq_no", -1))

            if not (part_no or order_no or raw_element):
                continue

            seq_no = self._to_int(seq_text)

            decl_brand = ""
            decl_model = ""
            if self.parser is not None and raw_element:
                try:
                    parsed = self.parser.parse(raw_element)
                    decl_brand = getattr(parsed, "brand", "") or ""
                    decl_model = getattr(parsed, "model", "") or ""
                except Exception as exc:  # noqa: BLE001 - 解析失败不中断（契约）
                    _logger.warning("要素解析失败（已降级）：%s", exc)

            records.append(
                DeclarationRecord(
                    ticket_no=ticket_no,
                    part_no=part_no,
                    order_no=order_no,
                    product_name=product_name,
                    seq_no=seq_no,
                    raw_element_text=raw_element,
                    decl_brand=decl_brand,
                    decl_model=decl_model,
                    structure_variant=variant,
                    source_row=row_index + 1,  # 1-based，便于人工回溯
                )
            )
        return records

    @staticmethod
    def _cell(texts: Sequence[str], col_index: int) -> str:
        """安全取列值（越界返回空串）。"""
        if col_index < 0 or col_index >= len(texts):
            return ""
        return _as_text(texts[col_index]).strip()

    @staticmethod
    def _to_int(value: str) -> int:
        """尽力把文本转为 int（失败返回 0）。"""
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0


def probe_excel(
    path: str | Path,
    parser: Any | None = None,
    header_scan_rows: int = HEADER_SCAN_ROWS,
) -> ExcelProbeResult:
    """便捷函数：一行完成探查（等价于 ``ExcelProbe(parser).probe(path)``）。

    Args:
        path: Excel 路径。
        parser: 可选的要素解析器。
        header_scan_rows: 表头扫描行数上限。

    Returns:
        :class:`core.models.ExcelProbeResult`。
    """
    return ExcelProbe(parser=parser, header_scan_rows=header_scan_rows).probe(path)
