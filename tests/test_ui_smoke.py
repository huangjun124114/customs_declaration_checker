"""T05 UI 层单测（tests/test_ui_smoke.py）。

在 ``QT_QPA_PLATFORM=offscreen`` 下验证三区 + 复核工作台的**纯 UI 行为**：

  * ``MainWindow`` 可构造 / 显示 / 关闭（T01 验收 ② 的 T05 加强版）；
  * **按钮状态机**（禁令 2）：无断点「▶ 开始」/ 有断点「↻ 重新开始」/ 暂停「▶ 继续执行」；
  * **断点红字**（禁令 2）：仅未完成断点显红字、色号 ``#D32F2F``、非匹配不显；
  * 进度事件驱动「N/M 条」+ 四卡计数之和 == 已处理；
  * 日志级别过滤**实时生效**；
  * 复核工作台重判幂等（经主窗口槽写回结果集）；
  * ``ui/styles/palette.py`` 的四色 + 断点红常量正确。

无显示环境（无 Qt / 无 offscreen）时整体 skip。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="无 PySide6，跳过 UI 用例")

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication（offscreen）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把「运行目录锚点」与「用户配置目录」隔离到 tmp（v0.2.0 点 1 引入的自动建目录）。

    否则构造 ``MainWindow`` 会在工程根下生成 ``报关申报要素校验/{result,logs}``、
    并把配置写进真实 ``%APPDATA%``。
    """
    import app.path_policy as path_policy

    monkeypatch.setattr(path_policy, "app_base_dir", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


@pytest.fixture
def window(qapp):
    """构造并显示一个 MainWindow（offscreen，保证 isVisible 语义正确）。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    win.show()
    yield win
    win.close()


# ══════════════════════════════════════════════════════════════════
#  palette 常量
# ══════════════════════════════════════════════════════════════════


def test_breakpoint_red_constant() -> None:
    """断点红常量逐字为 #D32F2F（架构 12.A）。"""
    from ui.styles.palette import BREAKPOINT_RED

    assert BREAKPOINT_RED == "#D32F2F"


def test_verdict_colors_cover_all_four() -> None:
    """四类判定均有展示色。"""
    from core.constants import ALL_VERDICTS, VERDICT_TEXT
    from ui.styles.palette import VERDICT_COLORS

    for verdict in ALL_VERDICTS:
        assert VERDICT_TEXT[verdict] in VERDICT_COLORS


# ══════════════════════════════════════════════════════════════════
#  主窗口结构
# ══════════════════════════════════════════════════════════════════


def test_main_window_three_zones_and_workbench(window) -> None:
    """三区 + 复核工作台齐备（2 个页签）。"""
    assert window.data_source_panel is not None
    assert window.progress_panel is not None
    assert window.summary_panel is not None
    assert window.workbench is not None
    assert window.tabs.count() == 2


# ══════════════════════════════════════════════════════════════════
#  禁令 2：按钮状态机
# ══════════════════════════════════════════════════════════════════


def test_button_state_machine_no_breakpoint(window) -> None:
    """无断点：开始按钮 =「▶ 开始」。"""
    from app.run_controller import ControllerState

    window.data_source_panel.set_state(ControllerState.IDLE_NO_BREAKPOINT)
    assert window.data_source_panel.btn_start.text() == "▶ 开始"


def test_button_state_machine_with_breakpoint(window) -> None:
    """有断点：开始按钮 =「↻ 重新开始」。"""
    from app.run_controller import ControllerState

    window.data_source_panel.set_state(ControllerState.IDLE_WITH_BREAKPOINT)
    assert window.data_source_panel.btn_start.text() == "↻ 重新开始"


def test_button_state_machine_paused(window) -> None:
    """暂停：暂停按钮 =「▶ 继续执行」。"""
    from app.run_controller import ControllerState

    window.data_source_panel.set_state(ControllerState.PAUSED)
    assert window.data_source_panel.btn_pause.text() == "▶ 继续执行"


# ══════════════════════════════════════════════════════════════════
#  禁令 2：断点红字
# ══════════════════════════════════════════════════════════════════


def test_breakpoint_hint_red_only_when_unfinished(window) -> None:
    """未完成断点 → 显红字且色号 #D32F2F。"""
    from app.events import BreakpointSummary

    summary = BreakpointSummary(matched=True, done_count=5, total_count=18, last_ts="2025-09-16T13:32")
    window.data_source_panel.show_breakpoint_hint(summary.hint_text())
    assert window.data_source_panel.breakpoint_hint.isVisible()
    assert "未完成断点" in window.data_source_panel.breakpoint_hint.text()
    assert "5/18" in window.data_source_panel.breakpoint_hint.text()
    assert "#d32f2f" in window.data_source_panel.breakpoint_hint.styleSheet().lower()


def test_breakpoint_hint_cleared_when_none(window) -> None:
    """无未完成断点 → 红字清除（不残留）。"""
    window.data_source_panel.show_breakpoint_hint("")
    assert window.data_source_panel.breakpoint_hint.text() == ""
    assert not window.data_source_panel.breakpoint_hint.isVisible()


# ══════════════════════════════════════════════════════════════════
#  进度事件 → N/M + 四卡
# ══════════════════════════════════════════════════════════════════


def test_progress_updates_counts_and_review(window) -> None:
    """进度事件驱动「N/M 条」+ 四卡 + 待复核。"""
    from app.events import TaskPhase, TaskProgress

    counts = {"PASS": 15, "FAIL": 0, "NO_MARK": 3, "NO_IMAGE": 0}
    prog = TaskProgress.make(TaskPhase.OCR, current=18, total=18, counts=counts)
    window._on_progress(prog)  # noqa: SLF001
    assert window.progress_panel.count_label.text() == "18/18 条"
    assert "已处理：18/18" in window.summary_panel.processed_label.text()
    assert "待人工复核：3 条" in window.summary_panel.review_label.text()


# ══════════════════════════════════════════════════════════════════
#  日志级别过滤（实时）
# ══════════════════════════════════════════════════════════════════


def test_log_level_filter_live(window) -> None:
    """切换过滤级别立即影响可见日志。"""
    from infra.logger import LogLevel

    panel = window.progress_panel
    panel.clear()
    panel.append_log("调试", level=LogLevel.DEBUG.value)
    panel.append_log("信息", level=LogLevel.INFO.value)
    panel.append_log("警告", level=LogLevel.WARN.value)
    panel.append_log("错误", level=LogLevel.ERROR.value)
    assert panel.visible_count() == 4

    idx = panel.level_combo.findData(LogLevel.ERROR.value)
    panel.level_combo.setCurrentIndex(idx)
    assert panel.visible_count() == 1
    assert "错误" in panel.visible_text()

    idx_all = panel.level_combo.findData(LogLevel.DEBUG.value)
    panel.level_combo.setCurrentIndex(idx_all)
    assert panel.visible_count() == 4


# ══════════════════════════════════════════════════════════════════
#  复核工作台：重判幂等 + 写回
# ══════════════════════════════════════════════════════════════════


def test_workbench_rejudge_idempotent(window) -> None:
    """经主窗口槽重判：连续两次不同判定以最后一次为准。

    v0.2.0 点 2：输出目录控件已移除，改为运行目录约定派生（由 ``_isolate_runtime_dirs``
    隔离到 tmp），故本用例不再设置 ``process_edit`` / ``result_edit``。
    """
    from core.models import CheckResult, DeclarationRecord, ImageEvidence, Verdict

    window.data_source_panel.ticket_edit.setText("SA26090215")

    window.session.clear()
    rec = DeclarationRecord(ticket_no="SA26090215", part_no="N1", order_no="O1")
    res = CheckResult(
        key=rec.key(),
        record=rec,
        verdict=Verdict.NO_MARK,
        evidence_images=[ImageEvidence(image_path="", seq=1, exists=False, not_found=True)],
    )
    window.session.replace([res], ticket_no="SA26090215")
    window.workbench.set_session(window.session)

    window._on_verdict_applied(res.key, Verdict.PASS.value, "合格", False)  # noqa: SLF001
    assert window.session.get(res.key).verdict is Verdict.PASS
    window._on_verdict_applied(res.key, Verdict.FAIL.value, "改异常", False)  # noqa: SLF001
    assert window.session.get(res.key).verdict is Verdict.FAIL
    assert window.session.get(res.key).reviewer_note == "改异常"


def test_workbench_lists_all_records_with_status_counts(window) -> None:
    """v0.3.0：工作台默认列**全部**记录（不再是「只列待复核」）。

    「谁要复核」由状态标签（成功/待复核/缺图/失败，带数量）与头部
    「待复核：N 条」承载，避免把 ⚠️/🔵 之外的问题记录静默隐藏。
    """
    from core.models import CheckResult, DeclarationRecord, Verdict

    window.session.clear()
    results = []
    for idx, verdict in enumerate(
        [Verdict.PASS, Verdict.FAIL, Verdict.NO_MARK, Verdict.NO_IMAGE]
    ):
        rec = DeclarationRecord(ticket_no="T", part_no=f"P{idx}", order_no="O")
        results.append(CheckResult(key=rec.key(), record=rec, verdict=verdict))
    window.session.replace(results, ticket_no="T")
    window.workbench.set_session(window.session)
    assert window.workbench.record_table.rowCount() == 4
    assert window.workbench.review_pending_count() == 2
    assert window.workbench.status_buttons["all"].text() == "全部 4"
    assert window.workbench.status_buttons[Verdict.NO_MARK.value].text() == "待复核 1"


def test_image_viewer_missing_image_shows_hint(window) -> None:
    """🔵 缺图：查看器不加载图片，只显提示。"""
    viewer = window.workbench.image_viewer
    ok = viewer.load("")
    assert ok is False
    assert viewer.image_label.pixmap().isNull()
    assert "无图片证据" in viewer.image_label.text()
