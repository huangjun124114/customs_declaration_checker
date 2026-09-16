"""v0.2.0 点 2/3/4：UI 运行期管控（控件精简 / 数据源锁定 / 工作台门禁）。

在 ``QT_QPA_PLATFORM=offscreen`` 下验证：

  * **点 2**：票号改为只读；成果 / 过程产出目录控件已移除；``inputs()`` 仍返回 5 键
    （输出目录为派生值）；``_ready_to_run()`` 只校验 2 项必填；
  * **点 3**：``RUNNING`` / ``PAUSED`` 期间 Excel / 图片根 输入框 + 浏览按钮 + 预检按钮
    全部置灰；``IDLE*`` 恢复可用；
  * **点 4**：运行中即使有待复核项，工作台按钮仍禁用；``FINISHED`` 后启用；
    工作台页签在 ``RUNNING`` 隐藏、``FINISHED`` 显示、``ABORTED`` 有结果显示 / 无结果隐藏。

无 PySide6 环境整体 skip。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="无 PySide6，跳过 UI 用例")

from app.run_controller import ControllerState  # noqa: E402
from core.models import CheckResult, DeclarationRecord, Verdict  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication（offscreen）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_runtime_dirs(tmp_path, monkeypatch):
    """把运行目录锚点 / 配置目录隔离到 tmp（避免污染工程根与 %APPDATA%）。"""
    import app.path_policy as path_policy

    monkeypatch.setattr(path_policy, "app_base_dir", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


@pytest.fixture
def window(qapp):
    """构造并显示一个 MainWindow（offscreen）。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    win.show()
    yield win
    win.close()


def _make_result(verdict: Verdict, part: str = "P1") -> CheckResult:
    """构造一条最小 CheckResult。"""
    record = DeclarationRecord(ticket_no="SA26090215", part_no=part, order_no="O1")
    return CheckResult(key=record.key(), record=record, verdict=verdict)


# ══════════════════════════════════════════════════════════════════
#  点 2：控件精简
# ══════════════════════════════════════════════════════════════════


def test_ticket_field_is_readonly(window) -> None:
    """票号输入框改为只读展示。"""
    assert window.data_source_panel.ticket_edit.isReadOnly() is True


def test_result_and_process_widgets_removed(window) -> None:
    """成果 / 过程产出目录控件整行移除。"""
    panel = window.data_source_panel
    assert not hasattr(panel, "result_edit")
    assert not hasattr(panel, "process_edit")


def test_browse_buttons_are_instance_attributes(window) -> None:
    """两个「浏览…」按钮已提升为实例属性（点 3 置灰所需）。"""
    panel = window.data_source_panel
    assert panel.excel_btn is not None
    assert panel.share_btn is not None
    assert panel.probe_btn is not None


def test_inputs_returns_five_keys_with_derived_dirs(window, tmp_path) -> None:
    """``inputs()`` 仍返回 5 键；输出目录为派生值（运行目录约定）。"""
    inputs = window.data_source_panel.inputs()
    assert set(inputs) == {
        "excel_path",
        "share_root",
        "ticket_no",
        "result_dir",
        "process_dir",
    }
    assert inputs["result_dir"] == str(tmp_path / "报关申报要素校验" / "result")
    assert inputs["process_dir"] == str(tmp_path / "报关申报要素校验" / "logs")


def test_ready_to_run_false_with_excel_only(window) -> None:
    """只有 Excel → 未就绪（``_ready_to_run`` 返回 None）。"""
    panel = window.data_source_panel
    panel.excel_edit.setText(r"D:\x.xlsx")
    panel.share_edit.setText("")
    assert window._ready_to_run() is None  # noqa: SLF001


def test_ready_to_run_true_with_excel_and_share(window) -> None:
    """Excel + 图片根 → 就绪。"""
    panel = window.data_source_panel
    panel.excel_edit.setText(r"D:\x.xlsx")
    panel.share_edit.setText(r"\\srv\2026年报关要素图片\张三\SA26090215")
    inputs = window._ready_to_run()  # noqa: SLF001
    assert inputs is not None
    assert inputs["excel_path"].endswith("x.xlsx")
    assert inputs["share_root"].endswith("SA26090215")


# ══════════════════════════════════════════════════════════════════
#  点 3：运行期锁定数据源
# ══════════════════════════════════════════════════════════════════

_LOCKED_WIDGETS = ("excel_edit", "excel_btn", "share_edit", "share_btn", "probe_btn")


def test_inputs_locked_while_running(window) -> None:
    """``RUNNING`` → Excel / 图片根 输入框 + 浏览按钮 + 预检按钮全部置灰。"""
    panel = window.data_source_panel
    panel.set_state(ControllerState.RUNNING)
    for name in _LOCKED_WIDGETS:
        widget = getattr(panel, name)
        assert widget.isEnabled() is False, f"{name} 应在 RUNNING 时置灰"


def test_inputs_locked_while_paused(window) -> None:
    """``PAUSED`` 也锁定（断点已绑定当前数据源指纹，换源会串票）。"""
    panel = window.data_source_panel
    panel.set_state(ControllerState.PAUSED)
    for name in _LOCKED_WIDGETS:
        widget = getattr(panel, name)
        assert widget.isEnabled() is False, f"{name} 应在 PAUSED 时置灰"


def test_inputs_unlocked_when_idle(window) -> None:
    """``IDLE*`` → 全部恢复可用。"""
    panel = window.data_source_panel
    panel.set_state(ControllerState.RUNNING)
    panel.set_state(ControllerState.IDLE_NO_BREAKPOINT)
    for name in _LOCKED_WIDGETS:
        widget = getattr(panel, name)
        assert widget.isEnabled() is True, f"{name} 应在 IDLE 时可用"


def test_inputs_unlocked_after_finished(window) -> None:
    """``FINISHED`` → 全部恢复可用（可换源再跑）。"""
    panel = window.data_source_panel
    panel.set_state(ControllerState.RUNNING)
    panel.set_state(ControllerState.FINISHED)
    for name in _LOCKED_WIDGETS:
        widget = getattr(panel, name)
        assert widget.isEnabled() is True, f"{name} 应在 FINISHED 时可用"


# ══════════════════════════════════════════════════════════════════
#  点 4：工作台按钮（叠加运行状态）
# ══════════════════════════════════════════════════════════════════

_COUNTS_WITH_REVIEW = {"PASS": 0, "FAIL": 0, "NO_MARK": 3, "NO_IMAGE": 0}
_COUNTS_NO_REVIEW = {"PASS": 5, "FAIL": 0, "NO_MARK": 0, "NO_IMAGE": 0}


def test_workbench_button_disabled_while_running(window) -> None:
    """运行中即使 ``review_total > 0``，工作台按钮仍禁用（修复既有缺陷）。"""
    panel = window.summary_panel
    panel.set_run_state(ControllerState.RUNNING)
    panel.apply_counts(_COUNTS_WITH_REVIEW, processed=3, total=3)
    assert panel.btn_workbench.isEnabled() is False


def test_workbench_button_enabled_after_finished(window) -> None:
    """``FINISHED`` 且有待复核项 → 按钮可用。"""
    panel = window.summary_panel
    panel.set_run_state(ControllerState.FINISHED)
    panel.apply_counts(_COUNTS_WITH_REVIEW, processed=3, total=3)
    assert panel.btn_workbench.isEnabled() is True


def test_workbench_button_disabled_without_review_items(window) -> None:
    """终态但无待复核项 → 按钮禁用。"""
    panel = window.summary_panel
    panel.set_run_state(ControllerState.FINISHED)
    panel.apply_counts(_COUNTS_NO_REVIEW, processed=5, total=5)
    assert panel.btn_workbench.isEnabled() is False


# ══════════════════════════════════════════════════════════════════
#  点 4：工作台页签可见性
# ══════════════════════════════════════════════════════════════════

_WORKBENCH_TAB = 1


def test_tab_initially_hidden(window) -> None:
    """初始隐藏工作台页签。"""
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is False


def test_tab_hidden_while_running(window) -> None:
    """``RUNNING`` 期间隐藏（即便已有结果）。"""
    window.session.replace([_make_result(Verdict.NO_MARK)], ticket_no="SA26090215")
    window._on_state(ControllerState.RUNNING)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is False


def test_tab_hidden_while_paused(window) -> None:
    """``PAUSED`` 期间隐藏。"""
    window.session.replace([_make_result(Verdict.NO_MARK)], ticket_no="SA26090215")
    window._on_state(ControllerState.PAUSED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is False


def test_tab_visible_after_finished(window) -> None:
    """``FINISHED`` 后可见。"""
    window.session.replace([_make_result(Verdict.PASS)], ticket_no="SA26090215")
    window._on_state(ControllerState.FINISHED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is True


def test_tab_visible_after_abort_with_results(window) -> None:
    """``ABORTED`` 且已产出结果 → 可见（Q4-附：允许中止后进工作台）。"""
    window.session.replace([_make_result(Verdict.NO_MARK)], ticket_no="SA26090215")
    window._on_state(ControllerState.ABORTED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is True


def test_tab_hidden_after_abort_without_results(window) -> None:
    """``ABORTED`` 且无结果 → 保持隐藏。"""
    window.session.clear()
    window._on_state(ControllerState.ABORTED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is False


def test_tab_hidden_after_failed_without_results(window) -> None:
    """``FAILED`` 且无结果 → 保持隐藏。"""
    window.session.clear()
    window._on_state(ControllerState.FAILED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is False


def test_tab_visible_after_failed_with_results(window) -> None:
    """``FAILED`` 且已产出结果 → 可见。"""
    window.session.replace([_make_result(Verdict.NO_MARK)], ticket_no="SA26090215")
    window._on_state(ControllerState.FAILED)  # noqa: SLF001
    assert window.tabs.isTabVisible(_WORKBENCH_TAB) is True
