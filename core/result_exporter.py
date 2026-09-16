"""三产物导出（core.result_exporter，对应架构设计第 4 节 + SOP 七 + 9.4 路径铁律）。

**三产物落盘规范**（架构设计 9.4，**违反即失败**）：

    ==============================  ==============  ====================================
    产物                             落点             文件名
    ==============================  ==============  ====================================
    汇总表（13 列）                  成果产出         ``校验汇总表_{票号}.xlsx``
    待人工复核清单                    成果产出         ``{票号}_待人工复核清单.csv``（utf-8-sig）
    详细日志 JSON                    过程产出         ``校验详细日志_{票号}_累积.json``
    ==============================  ==============  ====================================

**红线**：
  * 写入前断言目标目录归属，越界抛 :class:`infra.errors.OutputPathViolation`
    （:class:`core.result_exporter.ResultExporter` 在导入时即绑定 ``allowed_root``）。
  * **完整 OCR 原文只进 JSON 证据文件，不进日志**（架构设计 9.3）。
  * 汇总表**恰好 13 列**、顺序固定（用 ``core.constants.COLUMNS`` 断言）。
  * CSV 使用 ``utf-8-sig``（带 BOM）防 Excel 乱码。
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

from core import constants
from core.models import CheckResult
from infra.encoding import normalize_path
from infra.fs_lock import assert_within
from infra.logger import Phase, get_logger

__all__ = [
    "SUMMARY_PREFIX",
    "REVIEW_CSV_SUFFIX",
    "DETAIL_JSON_PREFIX",
    "DETAIL_JSON_SUFFIX",
    "REVIEW_COLUMNS",
    "ResultExporter",
]


#: 汇总表文件名前缀
SUMMARY_PREFIX: str = "校验汇总表"
#: 待人工复核清单文件名后缀
REVIEW_CSV_SUFFIX: str = "_待人工复核清单.csv"
#: 详细日志 JSON 文件名前缀
DETAIL_JSON_PREFIX: str = "校验详细日志"
#: 详细日志 JSON 文件名后缀
DETAIL_JSON_SUFFIX: str = "_累积.json"

#: 待人工复核清单列（问题说明在最后，便于人工填写）
REVIEW_COLUMNS: list[str] = [
    "出货通知书号",
    "成品料号",
    "订单号",
    "中文品名",
    "申报品牌",
    "申报型号",
    "图片识别品牌",
    "图片识别型号",
    "校验结果",
    "问题说明",
    "图片路径",
]


class ResultExporter:
    """三产物导出器（架构设计第 4 节 ``ResultExporter``）。

    Args:
        result_dir: **成果产出**目录（汇总表 + 复核清单只允许写入此处）。
        process_dir: **过程产出**目录（JSON 日志只允许写入此处）。
        allowed_result_root: 成果产出根的校验用根目录；缺省用 ``result_dir`` 自身。
        allowed_process_root: 过程产出根的校验用根目录；缺省用 ``process_dir`` 自身。

    Raises:
        OutputPathViolation: 构造时若 ``result_dir`` 为受保护输入（防御式）。
    """

    #: 汇总表列名（单一事实来源：``core.constants.COLUMNS``）
    COLUMNS: list[str] = constants.COLUMNS

    def __init__(
        self,
        result_dir: str | os.PathLike[str],
        process_dir: str | os.PathLike[str],
        *,
        allowed_result_root: str | os.PathLike[str] | None = None,
        allowed_process_root: str | os.PathLike[str] | None = None,
    ) -> None:
        """构造导出器。"""
        self.result_dir: Path = normalize_path(result_dir)
        self.process_dir: Path = normalize_path(process_dir)
        self._result_root: Path = (
            normalize_path(allowed_result_root)
            if allowed_result_root is not None
            else self.result_dir
        )
        self._process_root: Path = (
            normalize_path(allowed_process_root)
            if allowed_process_root is not None
            else self.process_dir
        )
        self._log = get_logger(Phase.EXPORT)

    # ══════════════════════════════════════════════════════════
    #  产物 1：汇总表（13 列）
    # ══════════════════════════════════════════════════════════

    def summary_path(self, ticket_no: str) -> Path:
        """返回汇总表目标路径 ``{成果产出}/校验汇总表_{票号}.xlsx``。"""
        return self.result_dir / f"{SUMMARY_PREFIX}_{_safe_ticket(ticket_no)}.xlsx"

    def review_csv_path(self, ticket_no: str) -> Path:
        """返回待人工复核清单路径 ``{成果产出}/{票号}_待人工复核清单.csv``。"""
        return self.result_dir / f"{_safe_ticket(ticket_no)}{REVIEW_CSV_SUFFIX}"

    def detail_json_path(self, ticket_no: str) -> Path:
        """返回详细日志 JSON 路径 ``{过程产出}/校验详细日志_{票号}_累积.json``。"""
        return self.process_dir / f"{DETAIL_JSON_PREFIX}_{_safe_ticket(ticket_no)}{DETAIL_JSON_SUFFIX}"

    def export_summary(
        self,
        results: list[CheckResult],
        ticket_no: str,
        out_dir: str | os.PathLike[str] | None = None,
    ) -> Path:
        """导出校验汇总表（**恰好 13 列**，顺序固定）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            out_dir: 覆盖成果产出目录（缺省用构造时的 ``result_dir``）。

        Returns:
            落盘绝对路径 :class:`pathlib.Path`。

        Raises:
            OutputPathViolation: 目标越界（不在成果产出根内）。
        """
        target = self._resolve_target(out_dir, self.result_dir, self.summary_path(ticket_no))
        self._assert_result_within(target)

        try:
            from openpyxl import Workbook
        except ImportError as exc:  # pragma: no cover - 环境缺依赖时的明确报错
            raise ImportError(
                "导出汇总表需要 openpyxl（pip install openpyxl）。"
                "禁用 --no-deps 安装；若报空壳包请运行 python tools/check_env.py"
            ) from exc

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "校验汇总表"
        sheet.append(list(self.COLUMNS))

        row_count = 0
        for result in results:
            row = result.to_row()
            if len(row) != constants.COLUMN_COUNT:
                raise ValueError(
                    f"汇总表行数不等于 {constants.COLUMN_COUNT} 列（实际 {len(row)}），"
                    f"违反 SOP 七固定列口径"
                )
            sheet.append(row)
            row_count += 1

        target.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(str(target))
        self._log.info(
            f"汇总表已导出：{target}（{row_count} 行 × {len(self.COLUMNS)} 列）"
        )
        return target

    # ══════════════════════════════════════════════════════════
    #  产物 2：待人工复核清单（CSV，utf-8-sig）
    # ══════════════════════════════════════════════════════════

    def export_review_csv(
        self,
        results: list[CheckResult],
        ticket_no: str,
        out_dir: str | os.PathLike[str] | None = None,
    ) -> Path:
        """导出待人工复核清单（⚠️ + 🔵，UTF-8-sig 带 BOM）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            out_dir: 覆盖成果产出目录。

        Returns:
            落盘绝对路径。

        Raises:
            OutputPathViolation: 目标越界。
        """
        target = self._resolve_target(out_dir, self.result_dir, self.review_csv_path(ticket_no))
        self._assert_result_within(target)

        pending = [r for r in results if r.verdict in constants.REVIEW_VERDICTS]
        target.parent.mkdir(parents=True, exist_ok=True)

        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(REVIEW_COLUMNS)
        for result in pending:
            writer.writerow(self._review_row(result))

        # ⚠️ 中文 Excel 兼容：必须 utf-8-sig（带 BOM）
        with open(target, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(buffer.getvalue())

        self._log.info(f"待人工复核清单已导出：{target}（{len(pending)} 条）")
        return target

    def _review_row(self, result: CheckResult) -> list[str]:
        """构造复核清单一行（问题说明取 reason + 差异摘要）。"""
        record = result.record
        problem_parts: list[str] = []
        if result.reason:
            problem_parts.append(result.reason)
        if result.differences:
            problem_parts.extend(d.summary() for d in result.differences)
        problem = "；".join(p for p in problem_parts if p)

        return [
            record.ticket_no if record is not None else "",
            record.part_no if record is not None else "",
            record.order_no if record is not None else "",
            record.product_name if record is not None else "",
            record.decl_brand if record is not None else "",
            record.decl_model if record is not None else "",
            result.detected_brand,
            result.detected_model,
            constants.verdict_text(result.verdict),
            problem,
            result.image_paths,
        ]

    # ══════════════════════════════════════════════════════════
    #  产物 3：详细日志 JSON（含完整 OCR 原文）
    # ══════════════════════════════════════════════════════════

    def export_detail_json(
        self,
        results: list[CheckResult],
        ticket_no: str,
        proc_dir: str | os.PathLike[str] | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """导出详细日志 JSON（**含完整 OCR 原文**，只落过程产出）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            proc_dir: 覆盖过程产出目录。
            extra: 附加元信息（如指纹、统计），写入 JSON 顶层 ``meta``。

        Returns:
            落盘绝对路径。

        Raises:
            OutputPathViolation: 目标越界。
        """
        target = self._resolve_target(proc_dir, self.process_dir, self.detail_json_path(ticket_no))
        self._assert_process_within(target)

        counts = {v.value: 0 for v in constants.ALL_VERDICTS}
        for result in results:
            counts[result.verdict.value] = counts.get(result.verdict.value, 0) + 1

        payload: dict[str, Any] = {
            "schema": 2,
            "ticket_no": ticket_no,
            "generated_at": _now_iso(),
            "counts": counts,
            "total": len(results),
            "meta": extra or {},
            "results": [result.to_dict() for result in results],
        }

        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

        self._log.info(f"详细日志已导出：{target}（{len(results)} 条，含完整 OCR 原文）")
        return target

    # ══════════════════════════════════════════════════════════
    #  批量导出
    # ══════════════════════════════════════════════════════════

    def export_all(
        self,
        results: list[CheckResult],
        ticket_no: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Path]:
        """一次性导出三产物（汇总表 / 复核清单 / 详细日志）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            extra: 附加元信息（写入 JSON 的 ``meta``）。

        Returns:
            ``{"summary": Path, "review": Path, "detail": Path}``。
        """
        return {
            "summary": self.export_summary(results, ticket_no),
            "review": self.export_review_csv(results, ticket_no),
            "detail": self.export_detail_json(results, ticket_no, extra=extra),
        }

    # ══════════════════════════════════════════════════════════
    #  内部实现
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _resolve_target(
        override: str | os.PathLike[str] | None,
        default_dir: Path,
        default_target: Path,
    ) -> Path:
        """解析目标路径（``override`` 给定时按其目录 + 默认文件名重算）。"""
        if override is None:
            return default_target
        override_dir = normalize_path(override)
        return override_dir / default_target.name

    def _assert_result_within(self, target: Path) -> None:
        """断言汇总表 / 复核清单落在成果产出根内。"""
        assert_within(target, self._result_root)

    def _assert_process_within(self, target: Path) -> None:
        """断言 JSON / 断点落在过程产出根内。"""
        assert_within(target, self._process_root)

    # ─────────────────────── 断言辅助（公开，供测试与上层）───────────────────────

    @staticmethod
    def assert_target_in_result(target: str | os.PathLike[str], result_root: str | os.PathLike[str]) -> None:
        """断言目标在成果产出根内（供上层写入前自检）。

        Raises:
            OutputPathViolation: 越界。
        """
        assert_within(target, result_root)

    @staticmethod
    def assert_target_in_process(target: str | os.PathLike[str], process_root: str | os.PathLike[str]) -> None:
        """断言目标在过程产出根内（供上层写入前自检）。

        Raises:
            OutputPathViolation: 越界。
        """
        assert_within(target, process_root)


def _safe_ticket(ticket_no: str) -> str:
    """清洗票号中的非法文件名字符。"""
    raw = (ticket_no or "").strip()
    cleaned = "".join(ch for ch in raw if ch not in '\\/:*?"<>|')
    return cleaned or "UNKNOWN"


def _now_iso() -> str:
    """返回当前时间 ISO 8601 字符串。"""
    import datetime as _dt

    return _dt.datetime.now().replace(microsecond=0).isoformat()
