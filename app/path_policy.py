"""路径策略（app.path_policy，对应架构设计 9.4 三铁律 + 12.D.2 + 12.D.3）。

**三条铁律的代码层落点**：

  1. **原始输入只读** —— :meth:`PathPolicy.validate_inputs` 只读探测输入存在性与
     可读性；从不写入用户选定输入。
  2. **产物分流** —— :meth:`PathPolicy.resolve_outputs` 返回 ``(result, logs)``
     两个目录；写入前用 :func:`infra.fs_lock.assert_within` 断言归属，
     越界抛 :class:`infra.errors.OutputPathViolation`。
  3. **运行目录约定（v0.2.0）** —— 未显式指定输出目录时，默认落在
     ``app_base_dir()/报关申报要素校验/{result,logs}``（打包态 = exe 同目录，
     开发态 = 工程根），并在解析时 **幂等创建**（``mkdir(exist_ok=True)``，
     已存在则复用，绝不清理已有内容）；**绝不静默扫描默认共享目录**（SOP 2.2）。

另含：

  * :meth:`PathPolicy.resolve_share_root` —— ``{年份}``/票号层三级判定（12.D.2）；
  * :meth:`PathPolicy.probe_unc` —— UNC 可达性探测（12.D.3）；
  * :meth:`PathPolicy.clean_dialog_path` —— 清洗 ``QFileDialog`` 的 ``file:///`` 前缀。

**分层约束**：``app/``（L3）只依赖 ``core`` + ``infra``，**不得** import Qt。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from infra.encoding import normalize_path, normalize_path_str
from infra.errors import PathNotAccessibleError
from infra.fs_lock import assert_within
from infra.logger import Phase, get_logger
from infra.resources import app_base_dir

__all__ = [
    "WORKSPACE_DIR_NAME",
    "RESULT_DIR_NAME",
    "PROCESS_DIR_NAME",
    "TICKET_SEGMENT_PATTERN",
    "infer_ticket_no_from_path",
    "PreflightReport",
    "PathPolicy",
]


#: 运行目录根名（v0.2.0 点 1）。
#:
#: ⚠️ 是「报关申报要素**校**验」而非「检验」—— 与项目全称 / ``APP_TITLE`` 对齐
#: （现场反馈原文「检验」经确认为笔误，Q1 已决）。
WORKSPACE_DIR_NAME: str = "报关申报要素校验"
#: 成果产出子目录默认名（交付物：汇总表 + 复核清单）。
RESULT_DIR_NAME: str = "result"
#: 过程产出子目录默认名（过程文件：JSON / 断点 / 缓存 / 运行日志）。
PROCESS_DIR_NAME: str = "logs"

#: 「形如票号的目录末段」判定正则：0–4 个字母 + 6–14 位数字（如 ``SA26090215``）。
#:
#: ⚠️ 刻意**要求以纯数字结尾**，以排除订单号（``2660310M``）与料号
#: （``N011901-009350-001``）这类"看起来也像票据号"的目录名。
#:
#: ────────────────────────────────────────────────────────────────────────
#: 与 ``core.constants.TICKET_NO_PATTERN`` 的差异（**为何不能合并**，P2-1/P2-3）：
#:
#:   * :data:`TICKET_NO_PATTERN`（core/constants.py）是「从 Excel 标题行**提取**票号」
#:     的**宽松**正则：``出货通知[书]?号\\s*[:：]?\\s*(?P<ticket>[A-Za-z]{0,4}\\d{6,14}[A-Za-z0-9\\-]*)``。
#:     其尾部额外允许 ``[A-Za-z0-9\\-]*``，因为标题行常带后缀（如 ``SA26090215-01``），
#:     提取时需要吞掉。
#:   * 本 :data:`TICKET_SEGMENT_PATTERN` 是「**目录末段 == 票号**」的**严格**整段匹配
#:     （``re.fullmatch``），**要求以纯数字结尾**。
#:
#: **两者职责不同，不能合并**：若把本处改用宽松的 ``TICKET_NO_PATTERN``，则
#: ``2660310M``（订单号，以字母结尾）、``N011901-009350-001``（料号，含连字符）以及
#: 纯数字日期目录名 ``20260902`` 都会被整段误判为票号 —— 即 P2-2 记录的"误跑错票"
#: 风险来源。故本文件**刻意保留**独立且更严的判定。
#:
#: 回归锁：``tests/test_ticket_segment_pattern.py`` 断言「本正则拒绝订单号 / 料号形态，
#: 而 ``TICKET_NO_PATTERN`` 会接受它们」，固定二者的差异语义（防日后被"顺手合并"）。
#: ────────────────────────────────────────────────────────────────────────
TICKET_SEGMENT_PATTERN: str = r"^[A-Za-z]{0,4}\d{6,14}$"


def infer_ticket_no_from_path(path: str | os.PathLike[str] | None) -> str | None:
    """从「图片根目录末段」推断票号（Q2 兜底 · 第一步，纯逻辑、无 Qt）。

    现场图片目录约定为 ``…\\{年份}年报关要素图片\\{人员}\\{票号}\\``，故**末段通常即票号**。
    本函数仅做**保守推断**：命中 :data:`TICKET_SEGMENT_PATTERN` 才返回，否则返回 ``None``，
    由 UI 层决定是否弹出输入框让用户手填（**绝不直接报错阻止开始**）。

    Args:
        path: 图片根目录路径（可 UNC、可带引号、可带尾随分隔符）。

    Returns:
        末段形如票号时返回该段（去空白）；否则 ``None``。
    """
    if path is None:
        return None
    text = str(path).strip().strip('"').strip()
    if not text:
        return None
    # 归一化 ``file:///`` 前缀 + 去尾随分隔符后取末段（兼容 UNC 与 Windows/Linux 分隔符）
    cleaned = normalize_path_str(text).rstrip("/\\")
    if not cleaned:
        return None
    segment = re.split(r"[\\/]", cleaned)[-1].strip()
    if segment and re.fullmatch(TICKET_SEGMENT_PATTERN, segment):
        return segment
    return None


@dataclass
class PreflightReport:
    """数据源预检报告（SOP Phase 0 产出，架构设计第 4 节 ``PreflightReport``）。

    Attributes:
        ok: 是否可以开始跑批（``errors`` 为空即为 ``True``）。
        errors: 致命错误列表（面向用户的中文文案，含路径）。
        warnings: 警告列表（不阻塞，仅提示）。
        ticket_hint: 从 Excel 自动识别的票号（可能为空）。
        record_estimate: 预计记录条数（0 表示未知）。
        excel_path: 规范化后的 Excel 路径。
        share_root: 解析后的图片根（票号目录层）。
        variant: 结构变体字符串（如 ``"D"``）；未知为空串。
        sheet_name: 选中的要素表名。
        header_row: 表头行号（**0-based**；UI 展示需 +1）。
        notes: 探查说明（含变体降级提示）。
        warnings_count: 警告条数（便捷属性）。
    """

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ticket_hint: str = ""
    record_estimate: int = 0
    excel_path: str = ""
    share_root: str = ""
    variant: str = ""
    sheet_name: str = ""
    header_row: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def warnings_count(self) -> int:
        """警告条数。"""
        return len(self.warnings)

    def status_text(self) -> str:
        """返回预检状态行文本（绿色 ✓ / 红色 ✗ / 黄色 ⚠）。"""
        if not self.ok:
            return "✗ 预检未通过：" + (self.errors[0] if self.errors else "数据源不可用")
        if self.warnings:
            return f"✓ 预检通过（{len(self.warnings)} 条提示）"
        return "✓ 预检通过"

    def to_dict(self) -> dict[str, object]:
        """序列化为字典（供日志 / 调试）。"""
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "ticket_hint": self.ticket_hint,
            "record_estimate": self.record_estimate,
            "excel_path": self.excel_path,
            "share_root": self.share_root,
            "variant": self.variant,
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "notes": list(self.notes),
        }


class PathPolicy:
    """路径与产物分流策略（架构设计第 4 节 ``PathPolicy``）。

    Args:
        app_home: 应用主目录（**显式给出时**作为输出目录锚点；缺省改用
            :func:`infra.resources.app_base_dir`）。
        protected_roots: 受保护输入根列表（防误写输入，供 :meth:`assert_readonly`）。
    """

    def __init__(
        self,
        app_home: str | os.PathLike[str] | None = None,
        *,
        protected_roots: list[str | os.PathLike[str]] | None = None,
    ) -> None:
        """构造路径策略。

        Args:
            app_home: 应用主目录（**仅当其显式传入时**才用作输出目录锚点）。
            protected_roots: 受保护输入根列表。
        """
        #: 仅当调用方**显式**传入 ``app_home`` 时才用它当锚点；否则锚点用
        #: :func:`infra.resources.app_base_dir`（打包态 = exe 目录，开发态 = 工程根）。
        self.app_home: Path = normalize_path(app_home) if app_home else Path.cwd()
        self._app_home_explicit: bool = app_home is not None
        self._protected: list[str] = [
            str(p) for p in (protected_roots or []) if str(p).strip()
        ]
        self._log = get_logger(Phase.PHASE0)

    # ══════════════════════════════════════════════════════════
    #  ① 输入校验（只读红线）
    # ══════════════════════════════════════════════════════════

    def validate_inputs(
        self,
        excel_path: str | os.PathLike[str],
        share_root: str | os.PathLike[str],
        *,
        ticket_no: str = "",
    ) -> PreflightReport:
        """校验输入数据源（存在性 / 可读性 / 权限 / UNC 可达性）。

        本方法**只读探测**，绝不写入输入。任一输入不可用 → ``ok=False`` 且
        ``errors`` 含明确中文文案（含路径 + 排查建议），UI 据此禁止开始。

        Args:
            excel_path: 申报要素 Excel 路径（可 UNC，可含 ``file:///`` 前缀）。
            share_root: 图片根目录（可 UNC）。
            ticket_no: 用户手填的票号（可选，用于校验票号目录）。

        Returns:
            :class:`PreflightReport`。
        """
        report = PreflightReport(ok=True)

        # ── Excel ──
        excel = self.clean_dialog_path(excel_path)
        if not str(excel).strip():
            report.ok = False
            report.errors.append("未选择「申报要素 Excel」文件，请先选择数据源。")
        else:
            report.excel_path = str(excel)
            if not excel.exists():
                report.ok = False
                report.errors.append(
                    f"Excel 文件不存在或不可访问：{excel}\n"
                    "请确认路径拼写；若在共享盘，请确认已登录有权限的域账号且网络连通。"
                )
            elif not os.access(excel, os.R_OK):
                report.ok = False
                report.errors.append(
                    f"Excel 文件无读取权限：{excel}\n请用有权限的账号重新登录后重试。"
                )
            elif not excel.is_file():
                report.ok = False
                report.errors.append(f"所选 Excel 路径不是文件：{excel}")

        # ── 图片根 ──
        share = self.clean_dialog_path(share_root)
        if not str(share).strip():
            report.ok = False
            report.errors.append("未选择「图片根目录」，请先选择数据源。")
        else:
            report.share_root = str(share)
            if not self.probe_unc(share):
                report.ok = False
                report.errors.append(
                    f"图片根目录不可达或权限不足：{share}\n"
                    "可能原因：路径不存在、共享盘（UNC）断连，或权限不足。\n"
                    "建议：确认路径拼写；确认已用有权限的域账号登录；确认网络/共享盘已连通。"
                )

        for root in (report.excel_path, report.share_root):
            if root and root not in self._protected:
                self._protected.append(root)

        if report.ok:
            self._log.info(f"预检通过：Excel={report.excel_path} 图片根={report.share_root}")
        else:
            self._log.warning(f"预检未通过（{len(report.errors)} 项）：{report.errors[:1]}")
        return report

    # ══════════════════════════════════════════════════════════
    #  ② 共享根解析（{年份} / 票号层三级判定，12.D.2）
    # ══════════════════════════════════════════════════════════

    def resolve_share_root(
        self,
        selected: str | os.PathLike[str],
        ticket_no: str,
    ) -> Path:
        """把用户所选目录解析为**票号目录**（架构设计 12.D.2 三级判定）。

        判定顺序：

          1. 所选目录**直接包含** ``{票号}`` 子目录 → 视用户选到 ``{年份}年...`` 层，
             返回该 ``{票号}`` 子目录；
          2. 所选目录**本身**即 ``{票号}`` 目录（末段 == 票号）→ 直接用；
          3. 所选目录的下一层存在 ``{人员}\\{票号}``（或任意一层深）→ 自动下探；
          4. 均不满足 → 抛 :class:`infra.errors.PathNotAccessibleError`
             （明确文案，**不盲目全盘搜索**，守 SOP 2.2）。

        Args:
            selected: 用户选择的目录（已清洗 ``file:///`` 前缀）。
            ticket_no: 票号。

        Returns:
            解析出的票号目录 :class:`pathlib.Path`。

        Raises:
            PathNotAccessibleError: 目录不可达，或在所选目录下找不到票号目录。
        """
        base = self.clean_dialog_path(selected)
        ticket = (ticket_no or "").strip()

        if not base.exists():
            raise PathNotAccessibleError(
                f"share root not reachable: {base}", path=str(base)
            )

        if not ticket:
            # 无票号：直接返回所选目录，由 ImageResolver 自行处理
            return base

        # ② 所选目录本身即票号目录
        if base.name.strip().upper() == ticket.upper():
            return base

        base_upper = base.name.strip().upper()

        # ① 所选目录直接含 {票号} 子目录
        direct = self._find_child_dir(base, ticket)
        if direct is not None:
            self._log.info(f"图片根解析：所选目录下直接命中票号目录 → {direct}")
            return direct

        # ③ 下一层 {人员}\{票号} 自动下探（仅下探一层，遵循"不盲目搜索"）
        for child in self._safe_scandir(base):
            if not child.is_dir():
                continue
            if child.name.strip().upper() == base_upper:
                continue
            deeper = self._find_child_dir(child, ticket)
            if deeper is not None:
                self._log.info(f"图片根解析：在下一层自动下探命中票号目录 → {deeper}")
                return deeper

        # ④ 明确报错（不盲目全盘搜索）
        raise PathNotAccessibleError(
            f"ticket dir not found under {base}: {ticket}",
            user_message=(
                f"未在所选目录下找到票号 {ticket}，请选择到票号目录。\n"
                f"所选目录：{base}\n"
                "建议：直接选择到「…\\报关要素图片\\{人员}\\{票号}\\」这一层。"
            ),
            path=str(base),
        )

    def _find_child_dir(self, parent: Path, name: str) -> Path | None:
        """在 ``parent`` 下查找名为 ``name`` 的子目录（精确 → 忽略大小写）。"""
        exact = parent / name
        if exact.is_dir():
            return exact
        target = name.strip().upper()
        for child in self._safe_scandir(parent):
            if child.is_dir() and child.name.strip().upper() == target:
                return child
        return None

    # ══════════════════════════════════════════════════════════
    #  ③ UNC 可达性探测（12.D.3）
    # ══════════════════════════════════════════════════════════

    def probe_unc(self, path: str | os.PathLike[str]) -> bool:
        """探测目录是否可达（``os.scandir`` 首项试读）。

        用于区分「断连 / 权限不足」（不可达）与「路径不存在」：本方法对两者均返回
        ``False``（预检只需知道"不可用"）；运行期区分由 ``ImageResolver`` 负责。

        Args:
            path: 待探测路径。

        Returns:
            ``True`` 表示可枚举（可达且可读）。
        """
        target = self.clean_dialog_path(path)
        if not str(target).strip() or str(target) == ".":
            return False
        try:
            if not target.is_dir():
                return False
        except OSError:
            return False
        return self._scandir_ok(target)

    @staticmethod
    def _scandir_ok(path: Path) -> bool:
        """尝试用 ``os.scandir`` 取首项，成功即认为可达。"""
        try:
            with os.scandir(path) as it:
                next(it, None)
            return True
        except OSError:
            return False

    def _safe_scandir(self, path: Path) -> list[Path]:
        """安全列出子目录（失败返回空列表，不抛异常）。"""
        try:
            return [Path(entry.path) for entry in os.scandir(path)]
        except OSError:
            return []

    # ══════════════════════════════════════════════════════════
    #  ④ 产物分流（铁律二）
    # ══════════════════════════════════════════════════════════

    def resolve_outputs(
        self,
        *,
        result_dir: str | os.PathLike[str] | None = None,
        process_dir: str | os.PathLike[str] | None = None,
        base: str | os.PathLike[str] | None = None,
    ) -> tuple[Path, Path]:
        """解析 ``(成果产出 result, 过程产出 logs)`` 两个输出目录（强制分流）。

        **v0.2.0 运行目录约定（点 1）**：

          * **都不给**（无显式覆盖）→ 返回
            ``anchor/报关申报要素校验/{result, logs}``，其中 ``anchor`` = 显式
            ``base`` > 显式 ``app_home`` > :func:`infra.resources.app_base_dir`；
          * 只给 ``result_dir`` → 在其下建 ``logs`` 子目录；
          * 只给 ``process_dir`` → 在其下建 ``result`` 子目录；
          * 两个都给 → 分别使用（保留测试 / CLI 的显式覆盖能力）。

        解析出的两个目录都会**幂等创建**（``mkdir(parents=True, exist_ok=True)``）：
        已存在则直接复用，**绝不清理已有内容**。

        Args:
            result_dir: 显式指定的成果产出目录（缺省 ``None``）。
            process_dir: 显式指定的过程产出目录（缺省 ``None``）。
            base: 显式指定的锚点基目录（缺省 ``None``；用于覆盖 ``app_base_dir()``）。

        Returns:
            ``(result_dir, process_dir)`` 两个 :class:`pathlib.Path`（均已创建）。
        """
        workspace = self._resolve_anchor(base) / WORKSPACE_DIR_NAME

        result_text = str(result_dir or "").strip()
        process_text = str(process_dir or "").strip()

        if result_text:
            resolved_result = self.clean_dialog_path(result_text)
        elif process_text:
            resolved_result = self.clean_dialog_path(process_text) / RESULT_DIR_NAME
        else:
            resolved_result = workspace / RESULT_DIR_NAME

        if process_text:
            resolved_process = self.clean_dialog_path(process_text)
        elif result_text:
            resolved_process = self.clean_dialog_path(result_text) / PROCESS_DIR_NAME
        else:
            resolved_process = workspace / PROCESS_DIR_NAME

        self._ensure_dir(resolved_result)
        self._ensure_dir(resolved_process)
        return resolved_result, resolved_process

    def _resolve_anchor(self, base: str | os.PathLike[str] | None) -> Path:
        """确定输出目录锚点：显式 ``base`` > 显式 ``app_home`` > ``app_base_dir()``。"""
        if base is not None:
            return normalize_path(base)
        if self._app_home_explicit:
            return self.app_home
        return app_base_dir()

    def _ensure_dir(self, target: Path) -> None:
        """幂等创建目录（已存在则复用；失败仅告警，不阻断）。"""
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: PERF203 - 单次调用，无需优化
            self._log.warning(f"运行目录创建失败（已忽略）：{target} - {exc}")

    def assert_target_in_result(
        self,
        target: str | os.PathLike[str],
        result_root: str | os.PathLike[str],
    ) -> None:
        """断言目标落在成果产出根内。

        Raises:
            OutputPathViolation: 越界。
        """
        assert_within(target, result_root)

    def assert_target_in_process(
        self,
        target: str | os.PathLike[str],
        process_root: str | os.PathLike[str],
    ) -> None:
        """断言目标落在过程产出根内。

        Raises:
            OutputPathViolation: 越界。
        """
        assert_within(target, process_root)

    def assert_readonly(self, target: str | os.PathLike[str]) -> None:
        """断写目标**不得**是受保护输入（只读红线铁律一）。

        Args:
            target: 期望写入的目标路径。

        Raises:
            OutputPathViolation: 目标落在受保护输入根内。
        """
        from infra.fs_lock import assert_readonly as _assert_readonly

        _assert_readonly(target, self._protected)

    # ══════════════════════════════════════════════════════════
    #  ⑤ 工具
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def clean_dialog_path(path: str | os.PathLike[str]) -> Path:
        """清洗 ``QFileDialog`` 返回值并转为 :class:`pathlib.Path`。

        去除 ``file:///`` 前缀、引号、尾随分隔符；UNC 保持原样。

        Args:
            path: 原始路径。

        Returns:
            规范化 :class:`pathlib.Path`。
        """
        return normalize_path(path)

    @staticmethod
    def normalize_key_path(path: str | os.PathLike[str]) -> str:
        """返回用于指纹比对的规范化路径字符串（12.A.3）。"""
        return normalize_path_str(path)
