"""三产物导出（core.result_exporter，对应架构设计第 4 节 + SOP 七 + 9.4 路径铁律）。

**三产物落盘规范**（架构设计 9.4，**违反即失败**）：

    ==============================  ==============  ====================================
    产物                             落点             文件名
    ==============================  ==============  ====================================
    汇总表（13 列）                  成果产出         ``校验汇总表_{票号}.xlsx``
    待人工复核清单                    成果产出         ``{票号}_待人工复核清单.csv``（utf-8-sig）
    详细日志 JSON                    过程产出         ``校验详细日志_{票号}_累积.json``
    ==============================  ==============  ====================================

**v0.3.7 需求 4 —— 复核后版本（另存，不覆盖上表任何产物）**：

    ==================================  ==========  ==================================================
    产物                                 落点         文件名
    ==================================  ==========  ==================================================
    复核后汇总表（13 列 + 2 标识列）        成果产出      ``校验汇总表_{票号}_复核后.xlsx``
    复核后复核清单（11 列 + 2 标识列）      成果产出      ``{票号}_待人工复核清单_复核后.csv``
    复核后详细 JSON                      过程产出      ``{票号}_复核后.json``（由 ``ReviewStore`` 落盘）
    ==================================  ==========  ==================================================

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
    "REVIEWED_NAME_SUFFIX",
    "REVIEWED_EXTRA_COLUMNS",
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
#: 【v0.3.7 需求 4】**复核后版本**文件名中缀。
#:
#: ``校验汇总表_{票号}_复核后.xlsx`` / ``{票号}_待人工复核清单_复核后.csv`` ——
#: 与**原始系统产物**并列存在，**绝不覆盖**原始产物（用户裁定 2026-09-17：
#: 「重判结果不覆盖原系统产生的结果，生成一个复核后的版本」）。
REVIEWED_NAME_SUFFIX: str = "_复核后"

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

#: 【v0.3.7 需求 4】**仅复核后版本**追加的列（原 13 列 / 11 列结构**保持不变**）。
#:
#: ⚠️ **为什么加在另存文件而不是原汇总表**：13 列结构是**已冻结**的交付口径
#: （``constants.COLUMNS`` + ``COLUMN_COUNT``，SOP 七），不得增删。
#: 复核后版本是**独立文件**，因此可以安全地追加"这条是人工重判的"标识列 ——
#: 既满足"结果表中标识"，又不触碰冻结红线。
REVIEWED_EXTRA_COLUMNS: list[str] = ["人工复核", "原系统判定"]


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

    def summary_reviewed_path(self, ticket_no: str) -> Path:
        """返回**复核后版本**汇总表路径 ``{成果产出}/校验汇总表_{票号}_复核后.xlsx``。"""
        return (
            self.result_dir
            / f"{SUMMARY_PREFIX}_{_safe_ticket(ticket_no)}{REVIEWED_NAME_SUFFIX}.xlsx"
        )

    def review_csv_reviewed_path(self, ticket_no: str) -> Path:
        """返回**复核后版本**复核清单路径 ``{成果产出}/{票号}_待人工复核清单_复核后.csv``。"""
        return (
            self.result_dir
            / f"{_safe_ticket(ticket_no)}_待人工复核清单{REVIEWED_NAME_SUFFIX}.csv"
        )

    def detail_json_path(self, ticket_no: str) -> Path:
        """返回详细日志 JSON 路径 ``{过程产出}/校验详细日志_{票号}_累积.json``。"""
        return self.process_dir / f"{DETAIL_JSON_PREFIX}_{_safe_ticket(ticket_no)}{DETAIL_JSON_SUFFIX}"

    def export_summary(
        self,
        results: list[CheckResult],
        ticket_no: str,
        out_dir: str | os.PathLike[str] | None = None,
        *,
        reviewed: bool = False,
    ) -> Path:
        """导出校验汇总表（**恰好 13 列**，顺序固定）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            out_dir: 覆盖成果产出目录（缺省用构造时的 ``result_dir``）。
            reviewed: 【v0.3.7 需求 4】``True`` → 导出**复核后版本**
                （文件名加 ``_复核后`` 中缀，并在 13 列之后**追加**两列
                「人工复核 / 原系统判定」标识；**原始汇总表不受任何影响**）。

        Returns:
            落盘绝对路径 :class:`pathlib.Path`。

        Raises:
            OutputPathViolation: 目标越界（不在成果产出根内）。
        """
        default_target = (
            self.summary_reviewed_path(ticket_no)
            if reviewed
            else self.summary_path(ticket_no)
        )
        target = self._resolve_target(out_dir, self.result_dir, default_target)
        self._assert_result_within(target)

        try:
            from openpyxl import Workbook
        except ImportError as exc:  # pragma: no cover - 环境缺依赖时的明确报错
            raise ImportError(
                "导出汇总表需要 openpyxl（pip install openpyxl）。"
                "禁用 --no-deps 安装；若报空壳包请运行 python tools/check_env.py"
            ) from exc

        columns = list(self.COLUMNS) + (list(REVIEWED_EXTRA_COLUMNS) if reviewed else [])
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "校验汇总表"
        sheet.append(columns)

        row_count = 0
        for result in results:
            row = list(result.to_row())
            if len(row) != constants.COLUMN_COUNT:
                raise ValueError(
                    f"汇总表行数不等于 {constants.COLUMN_COUNT} 列（实际 {len(row)}），"
                    f"违反 SOP 七固定列口径"
                )
            if reviewed:
                row += self._reviewed_extra(result)
            sheet.append(row)
            row_count += 1

        target.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(str(target))
        self._log.info(
            f"{'复核后' if reviewed else ''}汇总表已导出：{target}"
            f"（{row_count} 行 × {len(columns)} 列）"
        )
        return target

    @staticmethod
    def _reviewed_extra(result: CheckResult) -> list[str]:
        """复核后版本的**追加列**取值（与 :data:`REVIEWED_EXTRA_COLUMNS` 一一对应）。

        ⚠️ 「人工复核」文案的唯一来源是 :meth:`core.models.CheckResult.manual_review_mark`
        （复核工作台「复核」列共用同一出口），此处不再自己拼字符串。
        """
        return [result.manual_review_mark(), result.original_verdict_text()]

    # ══════════════════════════════════════════════════════════
    #  产物 2：待人工复核清单（CSV，utf-8-sig）
    # ══════════════════════════════════════════════════════════

    def export_review_csv(
        self,
        results: list[CheckResult],
        ticket_no: str,
        out_dir: str | os.PathLike[str] | None = None,
        *,
        reviewed: bool = False,
    ) -> Path:
        """导出待人工复核清单（⚠️ + 🔵，UTF-8-sig 带 BOM）。

        Args:
            results: 校验结果列表。
            ticket_no: 票号。
            out_dir: 覆盖成果产出目录。
            reviewed: 【v0.3.7 需求 4】``True`` → 导出**复核后版本**
                （文件名加 ``_复核后`` 中缀，追加「人工复核 / 原系统判定」两列）。

        Returns:
            落盘绝对路径。

        Raises:
            OutputPathViolation: 目标越界。
        """
        default_target = (
            self.review_csv_reviewed_path(ticket_no)
            if reviewed
            else self.review_csv_path(ticket_no)
        )
        target = self._resolve_target(out_dir, self.result_dir, default_target)
        self._assert_result_within(target)

        pending = [r for r in results if r.verdict in constants.REVIEW_VERDICTS]
        target.parent.mkdir(parents=True, exist_ok=True)

        columns = list(REVIEW_COLUMNS) + (list(REVIEWED_EXTRA_COLUMNS) if reviewed else [])
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(columns)
        for result in pending:
            row = list(self._review_row(result))
            if reviewed:
                row += self._reviewed_extra(result)
            writer.writerow(row)

        # ⚠️ 中文 Excel 兼容：必须 utf-8-sig（带 BOM）
        with open(target, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(buffer.getvalue())

        self._log.info(
            f"{'复核后' if reviewed else ''}待人工复核清单已导出：{target}（{len(pending)} 条）"
        )
        return target

    def _review_row(self, result: CheckResult) -> list[str]:
        """构造复核清单一行。

        「问题说明」直接复用 :meth:`core.models.CheckResult.verdict_basis` ——
        与汇总表第 10 列「判定依据」**同源同文**（v0.3.6）。此前这里另写一遍
        ``reason + d.summary()``，会导致反引号重复：同一条依据既在 ``reason``
        里、又在 ``DifferenceDetail.note`` 里，拼出来同句两三遍。
        """
        record = result.record

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
            result.verdict_basis(),
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
        """一次性导出三产物（汇总表 / 复核清单 / 详细日志）—— **系统原始版本**。

        ⚠️ 本方法是**跑批完成**时的落盘路径，产出的是**系统原始结果**。
        人工复核后的**复核后版本**走 :meth:`export_reviewed_all`，
        **不会覆盖**本方法的产物（v0.3.7 需求 4）。

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

    def export_reviewed_all(
        self,
        results: list[CheckResult],
        ticket_no: str,
    ) -> dict[str, Path]:
        """导出**复核后版本**（v0.3.7 需求 4）：汇总表 + 复核清单（均带人工复核标识）。

        **红线：绝不覆盖原系统产物** —— 落点文件名统一带 ``_复核后`` 中缀，
        与 :meth:`export_all` 的三个原始产物**并列存在**，可随时对照
        "系统判成什么 / 人工改成什么"。

        Args:
            results: 复核后的完整结果集（``verdict`` 已被人工改判覆盖，
                但每条的原系统判定保留在 ``CheckResult.original_verdict``）。
            ticket_no: 票号。

        Returns:
            ``{"summary_reviewed": Path, "review_reviewed": Path}``。
        """
        return {
            "summary_reviewed": self.export_summary(results, ticket_no, reviewed=True),
            "review_reviewed": self.export_review_csv(results, ticket_no, reviewed=True),
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
