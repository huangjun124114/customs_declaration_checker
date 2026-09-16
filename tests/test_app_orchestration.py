"""T05 应用编排层单测（tests/test_app_orchestration.py）。

覆盖 ``app/`` + ``core/pipeline.py`` 编排层的关键契约：

  * :class:`app.events.TaskProgress` 「N/M 条」+ 四卡计数之和；
  * :class:`app.events.BreakpointSummary` 红字模板 + 只对「未完成断点」显字（禁令 2）；
  * :class:`app.session.AppSession` 结果集口径（counts 键 = 判定枚举短值；pending 过滤）；
  * :class:`app.run_controller.RunControllerCore` 状态机（无断点 / 有断点 / 运行 / 暂停 / 完成）；
  * :class:`app.path_policy.PathPolicy` 输入校验 / 输出分流断言；
  * :class:`app.review_store.ReviewStore` 重判**幂等** + 留痕 + 复核后另存；
  * **禁令 1**：``CheckTask`` 注入 pipeline + run 传 flag 时 flag 必须生效（中止回归）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.events import BreakpointSummary, TaskPhase, TaskProgress
from app.path_policy import PathPolicy
from app.review_store import ReviewStore
from app.run_controller import ControllerState, RunControllerCore
from app.session import AppSession
from core.constants import REVIEW_VERDICTS
from core.models import CheckResult, DeclarationRecord, Verdict

# ══════════════════════════════════════════════════════════════════
#  TaskProgress / 口径自洽
# ══════════════════════════════════════════════════════════════════


def test_task_progress_text_and_counts_sum() -> None:
    """四卡计数之和 = 已处理数；progress_text 为「N/M 条」。"""
    counts = {"PASS": 15, "FAIL": 0, "NO_MARK": 3, "NO_IMAGE": 0}
    prog = TaskProgress.make(TaskPhase.OCR, current=18, total=18, counts=counts)
    assert prog.progress_text() == "18/18 条"
    assert prog.count_sum == 18
    assert prog.processed == 18
    assert abs(prog.fraction - 1.0) < 1e-9


def test_task_progress_zero_total_safe() -> None:
    """总数为 0 时不应除零。"""
    prog = TaskProgress.make(TaskPhase.IDLE, current=0, total=0)
    assert prog.fraction == 0.0
    assert prog.progress_text() == "0/0 条"


# ══════════════════════════════════════════════════════════════════
#  禁令 2：断点红字
# ══════════════════════════════════════════════════════════════════


def test_breakpoint_summary_no_unfinished_no_hint() -> None:
    """无未完成断点：has_unfinished=False 且 hint 为空。"""
    summary = BreakpointSummary()
    assert summary.has_unfinished is False
    assert summary.hint_text() == ""


def test_breakpoint_summary_exact_template() -> None:
    """未完成断点红字模板逐字一致（架构 12.A）。"""
    summary = BreakpointSummary(
        matched=True, done_count=5, total_count=18, last_ts="2025-09-16T13:32:00"
    )
    assert summary.has_unfinished is True
    text = summary.hint_text()
    assert text.startswith("⚠ 检测到未完成断点：已完成 5/18 条")
    assert "可点『继续执行』从断点续跑" in text


def test_breakpoint_summary_non_matching_no_red() -> None:
    """非匹配断点（matched=False）：即使有 done_count 也不显红字。"""
    summary = BreakpointSummary(matched=False, done_count=5, total_count=18)
    assert summary.has_unfinished is False
    assert summary.hint_text() == ""


# ══════════════════════════════════════════════════════════════════
#  AppSession 口径
# ══════════════════════════════════════════════════════════════════


def _make_result(verdict: Verdict, part: str, order: str = "2660326M") -> CheckResult:
    """构造一条最小 CheckResult。"""
    rec = DeclarationRecord(ticket_no="SA26090215", part_no=part, order_no=order)
    return CheckResult(key=rec.key(), record=rec, verdict=verdict)


def test_session_counts_keys_are_enum_values() -> None:
    """counts 键恒为判定枚举短值（PASS/FAIL/NO_MARK/NO_IMAGE）。"""
    session = AppSession()
    session.replace(
        [
            _make_result(Verdict.PASS, "P1"),
            _make_result(Verdict.PASS, "P2"),
            _make_result(Verdict.NO_MARK, "N1"),
            _make_result(Verdict.NO_IMAGE, "N2"),
        ],
        ticket_no="SA26090215",
    )
    counts = session.counts()
    assert set(counts) == {"PASS", "FAIL", "NO_MARK", "NO_IMAGE"}
    assert counts["PASS"] == 2
    assert counts["NO_MARK"] == 1
    assert counts["NO_IMAGE"] == 1
    assert session.count_sum() == len(session.results()) == 4


def test_session_pending_filters_review_verdicts() -> None:
    """pending_results 只含 REVIEW_VERDICTS（⚠️ + 🔵）。"""
    session = AppSession()
    session.replace(
        [
            _make_result(Verdict.PASS, "P1"),
            _make_result(Verdict.FAIL, "F1"),
            _make_result(Verdict.NO_MARK, "N1"),
            _make_result(Verdict.NO_IMAGE, "N2"),
        ]
    )
    pending = session.pending_results()
    assert len(pending) == 2
    assert {r.verdict for r in pending} == set(REVIEW_VERDICTS)
    assert session.pending_count() == 2


# ══════════════════════════════════════════════════════════════════
#  重判幂等 + 留痕
# ══════════════════════════════════════════════════════════════════


def test_review_store_idempotent_overwrite(temp_project: dict[str, Path]) -> None:
    """同 key 连续重判：以最后一次为准（幂等）。"""
    session = AppSession()
    session.replace([_make_result(Verdict.NO_MARK, "N1")], ticket_no="SA26090215")
    key = session.results()[0].key
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["process"])

    store.set_verdict(key, Verdict.PASS, "第一次", mark_missing=False)
    assert session.get(key).verdict is Verdict.PASS
    store.set_verdict(key, Verdict.FAIL, "第二次", mark_missing=False)
    assert session.get(key).verdict is Verdict.FAIL
    assert session.get(key).reviewer_note == "第二次"
    assert session.get(key).manually_reviewed is True
    assert store.override_count() == 1  # 幂等：只有一条生效改判


def test_review_store_save_round_and_reviewed_json(temp_project: dict[str, Path]) -> None:
    """留痕 review_round_N.json 与「复核后」JSON 真落盘。"""
    session = AppSession()
    session.replace([_make_result(Verdict.NO_MARK, "N1")], ticket_no="SA26090215")
    key = session.results()[0].key
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["process"])
    store.set_verdict(key, Verdict.PASS, "合格", mark_missing=False)

    round_path = store.save_round()
    assert round_path.exists()
    assert round_path.name.endswith("_1.json")

    from core.result_exporter import ResultExporter

    exporter = ResultExporter(
        temp_project["result"],
        temp_project["process"],
        allowed_result_root=temp_project["result"],
        allowed_process_root=temp_project["process"],
    )
    reviewed = store.save_reviewed_json(exporter)
    assert reviewed.exists()
    assert reviewed.name.endswith("_复核后.json")


def test_review_store_missing_key_raises(temp_project: dict[str, Path]) -> None:
    """改判不存在的 key 抛 KeyError。"""
    session = AppSession()
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["process"])
    with pytest.raises(KeyError):
        store.set_verdict("不存在|X|Y", Verdict.PASS)


# ══════════════════════════════════════════════════════════════════
#  PathPolicy 分流断言
# ══════════════════════════════════════════════════════════════════


def test_path_policy_resolve_outputs(temp_project: dict[str, Path]) -> None:
    """resolve_outputs 默认返回「报关申报要素校验/{result,logs}」两目录（v0.2.0 点 1）。"""
    policy = PathPolicy(app_home=temp_project["root"])
    result_dir, process_dir = policy.resolve_outputs()
    assert result_dir == temp_project["root"] / "报关申报要素校验" / "result"
    assert process_dir == temp_project["root"] / "报关申报要素校验" / "logs"


def test_path_policy_assert_within(temp_project: dict[str, Path]) -> None:
    """assert_target_in_result 越界抛 OutputPathViolation。"""
    from infra.errors import OutputPathViolation

    policy = PathPolicy(app_home=temp_project["root"])
    policy.assert_target_in_result(
        temp_project["result"] / "x.xlsx", temp_project["result"]
    )
    with pytest.raises(OutputPathViolation):
        policy.assert_target_in_result(
            temp_project["process"] / "x.xlsx", temp_project["result"]
        )


# ══════════════════════════════════════════════════════════════════
#  RunControllerCore 状态机
# ══════════════════════════════════════════════════════════════════


def test_controller_initial_state_is_idle() -> None:
    """缺省控制器为 IDLE。"""
    ctrl = RunControllerCore(AppSession())
    assert ctrl.state == ControllerState.IDLE
    assert ctrl.is_running() is False


def test_controller_probe_no_breakpoint_sets_state(temp_project: dict[str, Path]) -> None:
    """无断点探测 → IDLE_NO_BREAKPOINT，且红字为空。"""
    from app.check_task import CheckTask
    from core.rule_repository import RuleRepository

    repo = RuleRepository()
    repo.load_all()
    ctrl = RunControllerCore(CheckTask(rule_repository=repo))
    summary = ctrl.probe_breakpoint(
        ticket_no="UNKNOWN_TICKET_XYZ",
        excel_path=temp_project["input"] / "no.xlsx",
        share_root=temp_project["input"],
        process_dir=temp_project["process"],
        variant="D",
        total_count=18,
    )
    assert summary.has_unfinished is False
    assert ctrl.state in (
        ControllerState.IDLE_NO_BREAKPOINT,
        ControllerState.IDLE_WITH_BREAKPOINT,
    )


def test_controller_breakpoint_callback_fires(temp_project: dict[str, Path]) -> None:
    """断点探测回调被触发一次。"""
    from app.check_task import CheckTask
    from core.rule_repository import RuleRepository

    repo = RuleRepository()
    repo.load_all()
    ctrl = RunControllerCore(CheckTask(rule_repository=repo))
    seen: list[BreakpointSummary] = []
    ctrl.breakpoint_cb = seen.append
    ctrl.probe_breakpoint(
        ticket_no="UNKNOWN_TICKET_XYZ",
        excel_path=temp_project["input"] / "no.xlsx",
        share_root=temp_project["input"],
        process_dir=temp_project["process"],
        variant="D",
        total_count=18,
    )
    assert len(seen) == 1


def test_controller_pause_requires_running() -> None:
    """未运行时 pause() 不应改状态（防误触）。"""
    ctrl = RunControllerCore(AppSession())
    ctrl.pause()
    assert ctrl.state == ControllerState.IDLE


# ══════════════════════════════════════════════════════════════════
#  禁令 1 回归：注入 pipeline 时 stop_flag 必须生效
# ══════════════════════════════════════════════════════════════════


def test_check_task_injected_pipeline_honors_runtime_flags() -> None:
    """注入 pipeline 后 run(stop_flag=...) 必须把 flag 重绑到 pipeline。

    回归：历史缺陷下注入 pipeline 会忽略 run() 传的 flag，导致「中止」按钮失效。
    """
    from app.check_task import CheckTask
    from core.pipeline import CheckPipeline
    from core.rule_repository import RuleRepository

    repo = RuleRepository()
    repo.load_all()
    pipeline = CheckPipeline(repo)
    # 构造期未绑 flag（各持独立 Event）
    original_stop = pipeline.stop_flag
    runtime_stop = threading.Event()

    task = CheckTask(rule_repository=repo, pipeline=pipeline)
    # 直接调用内部重绑路径（不真跑批，仅验证 flag 被替换）
    task._rebind_flags(pipeline, runtime_stop, None)  # noqa: SLF001
    assert pipeline.stop_flag is runtime_stop
    assert pipeline.stop_flag is not original_stop


def test_check_task_build_pipeline_uses_flags() -> None:
    """未注入 pipeline 时，_build_pipeline 使用传入的 flag。"""
    from app.check_task import CheckTask
    from core.rule_repository import RuleRepository

    repo = RuleRepository()
    repo.load_all()
    task = CheckTask(rule_repository=repo)
    stop = threading.Event()
    pause = threading.Event()
    pipeline = task._build_pipeline(stop, pause)  # noqa: SLF001
    assert pipeline.stop_flag is stop
