"""列名 → 内部字段映射 + 命中率自校正（core.field_mapping，对应架构设计 6.1–6.4 / P4）。

**P4 是本次设计最危险的发现**（架构设计 〇.P4）：Excel 列名与数据语义**交叉**：
``物料编号`` 列的值形如 ``N011901-007386-001``，``订单号`` 列的值形如 ``2660326M``；
而图片文件名 = ``2660326M&N011901-007386-001&001.jpg``（首段 = Excel「订单号」列值，
第二段 = Excel「物料编号」列值）。若按列名字面映射 → 0 命中 → **全票判"缺图"**
（正是 SOP README 6.1「图片全部报缺图」的根因）。

对策：**三通道 + 命中率探针自校正**（架构设计 6.3）

    ┌─ 通道 1（列名字面映射）   ``part_no ← 物料编号列``、``order_no ← 订单号列``
    ├─ 通道 2（结构反推映射）   解析共享目录 ``{票号}\\{X}\\{X}&{Y}&{序号}.jpg``
    │                          → 令 ``order_no ← X 列``、``part_no ← Y 列``（以目录结构为准）
    └─ 通道 3（命中率探针）     干跑匹配 N 条；命中率 < 阈值时**交换** ``part_no``/``order_no``
                              重试，取命中率高者，置 ``swapped=True`` 并记 WARN

**实施口诀（写入本文件，贯穿实现）**：

    先按列名猜 → 拿目录结构验 → 用命中率定

本模块**不含任何 Excel I/O**（openpyxl 只在 :mod:`core.excel_probe` 中使用），
便于纯单测；对样本数据的表头/行数据以 ``list[tuple[str, ...]]`` 注入。

依赖：``core.models`` / ``core.constants`` / ``infra.logger``（**只依赖 infra 与 core，不依赖 PySide6**）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core import constants as C
from core.models import FieldMapping
from infra.logger import Phase, get_logger

__all__ = [
    "ColumnCandidate",
    "ProbeSample",
    "MappingProbeResult",
    "FieldMappingBuilder",
    "normalize_header",
    "score_header_row",
    "guess_mapping_by_literal",
    "guess_mapping_by_structure",
    "parse_image_filename",
    "evaluate_mapping",
    "build_mapping",
    "DEFAULT_HIT_RATE_THRESHOLD",
]

_logger = get_logger(Phase.PHASE1)

#: 命中率探针阈值（低于该值 → 触发交换自校正）—— 架构设计 6.3 通道 3
DEFAULT_HIT_RATE_THRESHOLD: float = 0.30

#: 映射通道名（写入 :attr:`FieldMapping.channel`）
CHANNEL_LITERAL = "literal"
CHANNEL_STRUCTURE = "structure"
CHANNEL_PROBE = "probe"


# ══════════════════════════════════════════════════════════════════
#  辅助数据结构（纯逻辑，注入即可单测）
# ══════════════════════════════════════════════════════════════════


@dataclass
class ColumnCandidate:
    """表头列候选（某一列名 → 列索引 + 命中打分）。

    Attributes:
        col_index: 0-based 列索引。
        header_text: 归一化后的表头文本。
        field_name: 命中的内部字段名（``part_no`` / ``element_text`` …）。
        score: 命中打分（越大越可信）。
    """

    col_index: int = 0
    header_text: str = ""
    field_name: str = ""
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（调试用）。"""
        return {
            "col_index": self.col_index,
            "header_text": self.header_text,
            "field_name": self.field_name,
            "score": self.score,
        }


@dataclass
class ProbeSample:
    """命中率探针样本（一条记录的行数据 + 期望的图片归属）。

    ``expect_*`` 为 ``None`` 表示"该维度不参与判定"（例如该行数据里该列缺失）。

    Attributes:
        row: 该行的单元格文本（按列索引对齐的列表）。
        expect_order_token: 目录/文件名首段（``{X}``），来自共享目录结构。
        expect_part_token: 文件名第二段（``{Y}``）。
    """

    row: Sequence[str] = field(default_factory=tuple)
    expect_order_token: str = ""
    expect_part_token: str = ""


@dataclass
class MappingProbeResult:
    """命中率探针结果。

    Attributes:
        hit_rate: 命中率（0.0–1.0）。
        hits: 命中条数。
        total: 参与判定的样本条数。
        swapped: 是否已交换 ``part_no`` / ``order_no``。
        col_map: 最终生效的 ``{内部字段: 列索引}``。
        channel: 最终映射通道。
        notes: 过程说明（进日志）。
    """

    hit_rate: float = 0.0
    hits: int = 0
    total: int = 0
    swapped: bool = False
    col_map: dict[str, int] = field(default_factory=dict)
    channel: str = CHANNEL_LITERAL
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "hit_rate": self.hit_rate,
            "hits": self.hits,
            "total": self.total,
            "swapped": self.swapped,
            "col_map": dict(self.col_map),
            "channel": self.channel,
            "notes": list(self.notes),
        }


# ══════════════════════════════════════════════════════════════════
#  表头归一化与打分
# ══════════════════════════════════════════════════════════════════

#: 归一化时需剥离的不可见字符（与 rules/separators.yaml 语义一致，此处为纯逻辑兜底）
_INVISIBLE_CHARS: tuple[str, ...] = (
    "\u200b",
    "\u200c",
    "\u200d",
    "\ufeff",
    "\u00a0",
    "\u3000",
)


def normalize_header(text: Any) -> str:
    """归一化表头文本（剥离不可见字符 + 冒号 + 空白 + 大小写折叠）。

    保证 ``申报要素`` / ``申报要素 `` / ``申报要素：`` / ``申报要素:`` 归一化一致
    （架构设计 6.2：``申报要素`` 可能带空格或位于末列）。

    Args:
        text: 原始表头单元格值（可为 ``None`` / 数字）。

    Returns:
        归一化后的表头字符串（空值返回 ``""``）。
    """
    if text is None:
        return ""
    raw = str(text)
    for ch in _INVISIBLE_CHARS:
        raw = raw.replace(ch, "")
    for colon in C.DEFAULT_COLON_VARIANTS:
        raw = raw.replace(colon, "")
    # 折叠所有空白 + 大写（兼容 "No." / "no."）
    raw = re.sub(r"\s+", "", raw)
    return raw.strip().upper()


def _hints() -> dict[str, list[str]]:
    """返回归一化后的「内部字段 → 表头关键词」映射（来自 constants）。"""
    result: dict[str, list[str]] = {}
    for field_name, keywords in C.SHEET_HEADER_HINTS.items():
        result[field_name] = [normalize_header(kw) for kw in keywords]
    return result


#: 表头关键词 → 内部字段名（与 SHEET_HEADER_HINTS 的键对应）
_HINT_TO_FIELD: dict[str, str] = {
    "element_col": "element_text",
    "part_col": "part_no",
    "order_col": "order_no",
    "name_col": "product_name",
    "seq_col": "seq_no",
    "qty_col": "qty",
    "weight_col": "weight",
}

#: 内部字段重要度权重（用于表头行打分：要素列 + 料号类列最重要）
_FIELD_WEIGHT: dict[str, int] = {
    "element_text": 5,
    "part_no": 3,
    "order_no": 2,
    "product_name": 1,
    "seq_no": 1,
    "qty": 0,
    "weight": 0,
}


def _match_field(header_norm: str) -> str:
    """把归一化表头匹配到内部字段名。

    匹配优先级：**精确相等** > 前缀/包含（避免短关键词误命中，如 ``"No."``→``"NO"``
    不会误吃 ``"NOTES"``）。

    Args:
        header_norm: 归一化后的表头。

    Returns:
        内部字段名；未命中返回 ``""``。
    """
    if not header_norm:
        return ""
    hints = _hints()
    # 第一轮：精确相等
    for field_key, keywords in hints.items():
        for kw in keywords:
            if kw and header_norm == kw:
                return _HINT_TO_FIELD.get(field_key, "")
    # 第二轮：包含（要求关键词长度 ≥ 2，防止单字误命中）
    best_field = ""
    best_len = 0
    for field_key, keywords in hints.items():
        for kw in keywords:
            if len(kw) >= 2 and kw in header_norm and len(kw) > best_len:
                best_field = _HINT_TO_FIELD.get(field_key, "")
                best_len = len(kw)
    return best_field


def score_header_row(headers: Sequence[Any]) -> tuple[int, list[ColumnCandidate]]:
    """对一行表头打分（架构设计 1.C1：要素表识别用**列名打分矩阵**）。

    打分规则：命中的内部字段按 :data:`_FIELD_WEIGHT` 累加；
    **必须同时命中「申报要素」与「料号类列」** 才算有效要素表行
    （这是把 Sheet2 与 Sheet1 的箱明细区分开的关键 —— Sheet1 首列是
    ``出货通知书号``，但**没有**申报要素列）。

    Args:
        headers: 该行的单元格值序列。

    Returns:
        ``(total_score, candidates)``；``total_score`` 为 0 表示该行不像表头。
    """
    candidates: list[ColumnCandidate] = []
    total = 0
    has_element = False
    has_part = False

    for idx, raw in enumerate(headers):
        header_norm = normalize_header(raw)
        if not header_norm:
            continue
        field_name = _match_field(header_norm)
        if not field_name:
            continue
        weight = _FIELD_WEIGHT.get(field_name, 0)
        candidates.append(
            ColumnCandidate(
                col_index=idx,
                header_text=header_norm,
                field_name=field_name,
                score=weight,
            )
        )
        total += weight
        if field_name == "element_text":
            has_element = True
        if field_name == "part_no":
            has_part = True

    # 硬门槛：要素表表头必须同时含「申报要素」与「料号类列」
    if not (has_element and has_part):
        # 仍返回 candidates 供日志/调试，但分数归零表示"不是要素表表头"
        return 0, candidates

    return total, candidates


# ══════════════════════════════════════════════════════════════════
#  通道 1 · 列名字面映射
# ══════════════════════════════════════════════════════════════════


def guess_mapping_by_literal(headers: Sequence[Any]) -> dict[str, int]:
    """通道 1：按**列名字面**建立映射。

    Args:
        headers: 表头行的单元格值序列。

    Returns:
        ``{内部字段名: 列索引}``（未命中的字段不出现）。
    """
    col_map: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        header_norm = normalize_header(raw)
        if not header_norm:
            continue
        field_name = _match_field(header_norm)
        if field_name and field_name not in col_map:
            col_map[field_name] = idx
    return col_map


# ══════════════════════════════════════════════════════════════════
#  文件名解析 + 通道 2 · 结构反推映射
# ══════════════════════════════════════════════════════════════════

#: 图片文件名解析：``{A}&{B}&{C}`` （放宽容错，允许扩展名任意）
_FILENAME_RE = re.compile(r"^(?P<a>[^&]+)&(?P<b>[^&]+)&(?P<c>[^&.]+)")


def parse_image_filename(name: str) -> tuple[str, str, str]:
    """解析图片文件名 ``{A}&{B}&{C}.jpg``（架构设计 6.4 + SOP 3.2）。

    Args:
        name: 文件名（可含扩展名，可含路径 —— 只取文件名）。

    Returns:
        ``(A, B, C)``：``A`` = 首段（= ``order_no``，也 = 子目录名）、
        ``B`` = 第二段（= ``part_no``）、``C`` = 序号（**会跳号**）。
        无法解析时返回 ``("", "", "")``（不抛异常）。
    """
    if not name:
        return "", "", ""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    # 去掉扩展名
    if "." in base:
        base = base.rsplit(".", 1)[0]
    match = _FILENAME_RE.match(base)
    if match is None:
        return "", "", ""
    return match.group("a"), match.group("b"), match.group("c")


def _value_in_row(row: Sequence[Any], col_index: int) -> str:
    """安全取某列值（越界返回空串，并 strip + 大写化供比对）。"""
    if col_index < 0 or col_index >= len(row):
        return ""
    value = row[col_index]
    return "" if value is None else str(value).strip()


def guess_mapping_by_structure(
    literal_map: dict[str, int],
    struct_samples: Sequence[ProbeSample],
) -> tuple[dict[str, int], list[str]]:
    """通道 2：**以共享目录结构为准**反推 ``part_no`` / ``order_no`` 列。

    做法：对每个样本行，取 §6.4 的 ``A`` 段（order token）与 ``B`` 段（part token），
    在行内寻找**值相等的列**；把 ``A`` 命中的列判为 ``order_no``、
    ``B`` 命中的列判为 ``part_no`` —— **不盲信列名**（SOP 陷阱 #11）。

    Args:
        literal_map: 通道 1 的映射（用于继承其它字段，如 ``element_text``）。
        struct_samples: 结构样本（行数据 + A/B 段期望值）。

    Returns:
        ``(col_map, notes)``：更新后的映射与说明。若无法反推，原样返回
        ``literal_map``（不返回空映射，保证兜底）。
    """
    notes: list[str] = []
    if not literal_map:
        return {}, ["通道2：缺少通道1映射，无法反推"]

    vote_order: dict[int, int] = {}
    vote_part: dict[int, int] = {}

    for sample in struct_samples:
        row = sample.row
        a_token = (sample.expect_order_token or "").strip()
        b_token = (sample.expect_part_token or "").strip()
        for idx in range(len(row)):
            cell = _value_in_row(row, idx)
            if not cell:
                continue
            if a_token and cell == a_token:
                vote_order[idx] = vote_order.get(idx, 0) + 1
            if b_token and cell == b_token:
                vote_part[idx] = vote_part.get(idx, 0) + 1

    if not vote_order and not vote_part:
        notes.append("通道2：目录结构与行数据无交叉证据，退回通道1")
        return dict(literal_map), notes

    new_map = dict(literal_map)
    if vote_order:
        best_order = max(vote_order, key=lambda k: vote_order[k])
        new_map["order_no"] = best_order
        notes.append(
            f"通道2：目录首段反推 order_no → 列{best_order}"
            f"（票数 {vote_order[best_order]}）"
        )
    if vote_part:
        best_part = max(vote_part, key=lambda k: vote_part[k])
        new_map["part_no"] = best_part
        notes.append(
            f"通道2：文件名第二段反推 part_no → 列{best_part}"
            f"（票数 {vote_part[best_part]}）"
        )

    # 若反推结论与列名字面**不同**，记录 WARN 级说明（P4 场景）
    literal_part = literal_map.get("part_no", -1)
    literal_order = literal_map.get("order_no", -1)
    if new_map.get("part_no", -1) != literal_part or new_map.get("order_no", -1) != literal_order:
        notes.append(
            "通道2：列名语义与目录结构交叉（P4）—— part_no/order_no 以目录结构为准"
        )

    return new_map, notes


# ══════════════════════════════════════════════════════════════════
#  通道 3 · 命中率探针
# ══════════════════════════════════════════════════════════════════


def evaluate_mapping(
    col_map: dict[str, int],
    samples: Sequence[ProbeSample],
    swapped: bool = False,
) -> MappingProbeResult:
    """用样本干跑评估一个映射的**命中率**（架构设计 6.3 通道 3）。

    一条样本算"命中"当且仅当：``part_no`` 列值与 ``expect_part_token`` 相等，
    **且** （若有 ``expect_order_token``）``order_no`` 列值与其相等。

    Args:
        col_map: ``{内部字段: 列索引}``。
        samples: 探针样本。
        swapped: 该映射是否已执行过交换（透传结果）。

    Returns:
        :class:`MappingProbeResult`。
    """
    part_col = col_map.get("part_no", -1)
    order_col = col_map.get("order_no", -1)

    hits = 0
    total = 0
    for sample in samples:
        expect_part = (sample.expect_part_token or "").strip()
        expect_order = (sample.expect_order_token or "").strip()
        if not expect_part and not expect_order:
            continue
        total += 1

        ok = True
        if expect_part:
            ok = ok and (_value_in_row(sample.row, part_col) == expect_part)
        if ok and expect_order:
            ok = ok and (_value_in_row(sample.row, order_col) == expect_order)
        if ok:
            hits += 1

    hit_rate = (hits / total) if total > 0 else 0.0
    return MappingProbeResult(
        hit_rate=hit_rate,
        hits=hits,
        total=total,
        swapped=swapped,
        col_map=dict(col_map),
    )


def _swap_part_order(col_map: dict[str, int]) -> dict[str, int]:
    """交换映射中的 ``part_no`` / ``order_no`` 两列（P4 自校正核心动作）。"""
    swapped = dict(col_map)
    part_col = col_map.get("part_no", -1)
    order_col = col_map.get("order_no", -1)
    if part_col >= 0:
        swapped["order_no"] = part_col
    elif "order_no" in swapped:
        del swapped["order_no"]
    if order_col >= 0:
        swapped["part_no"] = order_col
    elif "part_no" in swapped:
        del swapped["part_no"]
    return swapped


# ══════════════════════════════════════════════════════════════════
#  顶层构建入口（三通道串联）
# ══════════════════════════════════════════════════════════════════


def build_mapping(
    headers: Sequence[Any],
    header_row: int,
    ticket_no: str = "",
    struct_samples: Sequence[ProbeSample] | None = None,
    probe_samples: Sequence[ProbeSample] | None = None,
    hit_rate_threshold: float = DEFAULT_HIT_RATE_THRESHOLD,
) -> FieldMapping:
    """三通道串联构建字段映射（架构设计 6.3，**P4 自校正的实现**）。

    流程：::

        通道1 列名字面映射
          └─ 通道2（有目录结构样本时）以结构反推 part/order
               └─ 通道3 命中率探针；命中率 < 阈值 → 交换 part/order 重试，取高者

    **口诀**：先按列名猜 → 拿目录结构验 → 用命中率定。

    Args:
        headers: 表头行的单元格值。
        header_row: 表头行号（0-based）。
        ticket_no: 票号（透传）。
        struct_samples: 用于通道 2 的结构样本（可为 ``None``）。
        probe_samples: 用于通道 3 的命中率探针样本（可为 ``None``）。
        hit_rate_threshold: 命中率阈值（低于则触发交换）。

    Returns:
        :class:`core.models.FieldMapping`。
    """
    # ── 通道 1 ──
    literal_map = guess_mapping_by_literal(headers)
    col_map = dict(literal_map)
    channel = CHANNEL_LITERAL
    notes: list[str] = [f"通道1：列名字面映射 {col_map}"]

    # ── 通道 2 ──
    if struct_samples:
        struct_map, struct_notes = guess_mapping_by_structure(col_map, struct_samples)
        notes.extend(struct_notes)
        if struct_map:
            col_map = struct_map
            if any("P4" in n for n in struct_notes):
                channel = CHANNEL_STRUCTURE

    # ── 通道 3 ──
    swapped = False
    hit_rate = 0.0
    if probe_samples:
        base_result = evaluate_mapping(col_map, probe_samples, swapped=False)
        best = base_result
        if base_result.hit_rate < hit_rate_threshold:
            swapped_map = _swap_part_order(col_map)
            swap_result = evaluate_mapping(swapped_map, probe_samples, swapped=True)
            notes.append(
                f"通道3：命中率探针 {base_result.hit_rate:.0%} < 阈值 "
                f"{hit_rate_threshold:.0%}，尝试交换 part_no/order_no → "
                f"{swap_result.hit_rate:.0%}"
            )
            if swap_result.hit_rate > base_result.hit_rate:
                best = swap_result
                swapped = True
                channel = CHANNEL_PROBE
                _logger.warning(
                    "字段映射命中率探针触发交换（P4 自校正）：%s → %s；"
                    "命中率 %.0f%% → %.0f%%",
                    base_result.col_map,
                    swap_result.col_map,
                    base_result.hit_rate * 100,
                    swap_result.hit_rate * 100,
                )
            else:
                notes.append("通道3：交换后命中率未提升，保留原映射")
        else:
            notes.append(f"通道3：命中率探针 {base_result.hit_rate:.0%} 达标，无需交换")

        col_map = best.col_map
        hit_rate = best.hit_rate
        if not swapped and hit_rate >= hit_rate_threshold:
            channel = CHANNEL_PROBE if channel == CHANNEL_LITERAL else channel

    return FieldMapping(
        col_map=col_map,
        header_row=header_row,
        ticket_no=ticket_no,
        hit_rate=hit_rate,
        swapped=swapped,
        channel=channel,
        notes=notes,
    )


class FieldMappingBuilder:
    """字段映射构建器（面向 :class:`core.excel_probe.ExcelProbe` 的门面）。

    把「表头行 + 行数据 + 目录结构样本」聚合成 :meth:`build` 的输入，
    使 ``excel_probe`` 无需关心三通道细节（**单一职责**）。

    Example:
        >>> builder = FieldMappingBuilder(ticket_no="SA26090215")
        >>> builder.set_headers(["序号", "物料编号", "订单号", "申报要素"], header_row=1)
        >>> mapping = builder.build()
        >>> mapping.resolve("part_no")   # 物料编号列
        1
    """

    def __init__(
        self,
        ticket_no: str = "",
        hit_rate_threshold: float = DEFAULT_HIT_RATE_THRESHOLD,
    ) -> None:
        """构造构建器。

        Args:
            ticket_no: 票号。
            hit_rate_threshold: 命中率探针阈值。
        """
        self.ticket_no: str = ticket_no
        self.hit_rate_threshold: float = hit_rate_threshold
        self._headers: list[Any] = []
        self._header_row: int = 0
        self._struct_samples: list[ProbeSample] = []
        self._probe_samples: list[ProbeSample] = []

    def set_headers(self, headers: Sequence[Any], header_row: int) -> FieldMappingBuilder:
        """设置表头行。

        Args:
            headers: 表头单元格值。
            header_row: 表头行号（0-based）。

        Returns:
            ``self``（链式调用）。
        """
        self._headers = list(headers)
        self._header_row = header_row
        return self

    def add_struct_sample(self, sample: ProbeSample) -> FieldMappingBuilder:
        """追加一个通道 2 结构样本。

        Args:
            sample: 结构样本（行数据 + A/B 段期望值）。

        Returns:
            ``self``（链式调用）。
        """
        self._struct_samples.append(sample)
        return self

    def add_probe_sample(self, sample: ProbeSample) -> FieldMappingBuilder:
        """追加一个通道 3 命中率探针样本。

        Args:
            sample: 探针样本。

        Returns:
            ``self``（链式调用）。
        """
        self._probe_samples.append(sample)
        return self

    def extend_rows(self, rows: Iterable[Sequence[Any]]) -> FieldMappingBuilder:
        """从行数据批量补充探针样本（无 A/B 期望值时仅作占位）。

        说明：无目录结构信息时无法判定期望 token，故本方法**仅在行内已有
        可用 token 时才生成有效样本**；``excel_probe`` 会显式构造带 token 的样本。

        Args:
            rows: 行数据序列。

        Returns:
            ``self``（链式调用）。
        """
        for row in rows:
            self._probe_samples.append(ProbeSample(row=list(row)))
        return self

    def build(self) -> FieldMapping:
        """执行三通道构建并返回 :class:`core.models.FieldMapping`。

        Returns:
            最终字段映射。
        """
        return build_mapping(
            headers=self._headers,
            header_row=self._header_row,
            ticket_no=self.ticket_no,
            struct_samples=self._struct_samples or None,
            probe_samples=self._probe_samples or None,
            hit_rate_threshold=self.hit_rate_threshold,
        )
