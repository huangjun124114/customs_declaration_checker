"""v0.3.7 四条需求回归锁（``tests/test_v037_workbench.py``）。

需求出处：现场复核人反馈（v0.3.6 已成包但未提交时提出）。四条需求分属三层，
故本文件**跨层断言**，逐条给出**可证伪**的回归锁：

  1. **切换条下沉 + 左右翻页**（``ui/widgets/image_viewer.py``）
     证据图切换按钮原挂在 ``WorkbenchCard``（**整卡纵向滚动**）里 —— 判定原因一长
     就被推到折叠线以下，现场"看不到切换按钮"。现改为常驻**图片正下方**，
     并新增 ``◀ 上一张 / 下一张 ▶``。
  2. **命中图按钮标红**（同上）：命中申报要素的图 → 红色按钮；其余 → 主题色。
  3. **命中图右上角红星**（同上）：``_ImageStage.paintEvent`` 叠加红色 ``★``。
  4. **人工重判标识 + 不覆盖原系统结果 + 复核后版本 + 保存即落盘**
     （``core/models.py`` / ``core/result_exporter.py`` / ``app/review_store.py``）。

**红线守则**：13 列汇总表冻结只约束**导出产物**（``constants.COLUMNS``）；
复核标识只出现在**复核后版本**（``_复核后`` 另存文件）与**工作台记录表**里，
系统原始产物**一字不动**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="无 PySide6，跳过 UI 用例")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QFrame, QLabel, QPushButton  # noqa: E402

from core.constants import COLUMN_COUNT, COLUMNS, MANUAL_REVIEW_MARK, VERDICT_TEXT  # noqa: E402
from core.models import (  # noqa: E402
    CheckResult,
    DeclarationRecord,
    ImageEvidence,
    OcrText,
    Verdict,
)
from core.result_exporter import (  # noqa: E402
    REVIEWED_EXTRA_COLUMNS,
    REVIEWED_NAME_SUFFIX,
    ResultExporter,
)
from core.token_matcher import (  # noqa: E402
    MATCH_EXACT,
    MATCH_FUZZY,
    MATCH_NONE,
    TokenMatch,
)
from ui.styles.palette import Palette  # noqa: E402
from ui.widgets.image_viewer import ImageViewer, _ImageStage, _switch_button_qss  # noqa: E402
from ui.widgets.review_workbench import ReviewWorkbench, hit_image_paths  # noqa: E402

# ══════════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════════


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
    """隔离运行目录锚点 / 配置目录，避免污染工程根与 %APPDATA%。"""
    import app.path_policy as path_policy

    monkeypatch.setattr(path_policy, "app_base_dir", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


def _ocr(text: str) -> OcrText:
    """构造一个 OcrText（简化夹具）。"""
    return OcrText(text_raw=text, confidence=0.9, seq=0)


def _evidence_result() -> CheckResult:
    """3 张图的记录；**图 2 命中品牌**（EXACT），另两张未命中。"""
    record = DeclarationRecord(
        ticket_no="SA26090215",
        part_no="N1",
        order_no="O1",
        decl_brand="baori",
        decl_model="A7A01G",
    )
    images = [
        ImageEvidence(image_path="C:/img/1.jpg", seq=1, exists=True, ocr=_ocr("AAA")),
        ImageEvidence(image_path="C:/img/2.jpg", seq=2, exists=True, ocr=_ocr("baori E339")),
        ImageEvidence(image_path="C:/img/3.jpg", seq=3, exists=True, ocr=_ocr("CCC")),
    ]
    return CheckResult(
        key=record.key(),
        record=record,
        verdict=Verdict.PASS,
        token_matches=[
            TokenMatch(
                field="品牌",
                declared="baori",
                mode=MATCH_EXACT,
                token="baori",
                images=[2],
                sample_line="baori E339",
            )
        ],
        evidence_images=images,
    )


def _session(result: CheckResult):
    """把单条结果放进会话。"""
    from app.session import AppSession

    session = AppSession()
    session.replace([result], ticket_no=result.record.ticket_no)
    return session


def _workbench(result: CheckResult) -> ReviewWorkbench:
    """构造工作台（单条记录，已选中）。"""
    return ReviewWorkbench(_session(result))


# ══════════════════════════════════════════════════════════════════
#  需求 1：切换条下沉到图片下方 + 左右翻页
# ══════════════════════════════════════════════════════════════════


def test_switcher_sits_inside_viewer_below_image(qapp) -> None:
    """需求 1：切换条是**查看器自身布局**的直系子项，且排在滚动区（图片）之后。

    这是"放在图片下面"的结构化定义 —— 只要它还在卡片里（哪怕是卡片的底部），
    整卡滚动就会再次把它推出视口。
    """
    viewer = ImageViewer()
    holder = viewer.findChild(QFrame, "imageSwitcher")
    assert holder is not None, "切换条必须存在"
    outer = viewer.layout()
    index_scroll = outer.indexOf(viewer.scroll)
    index_switcher = outer.indexOf(holder)
    assert index_scroll >= 0 and index_switcher >= 0
    assert index_switcher > index_scroll, "切换条必须排在图片（滚动区）之后 → 视觉上在图片下方"


def test_switcher_not_inside_workbench_card_scroll(qapp) -> None:
    """**根因锁**：切换条**不在**证据卡的滚动内容里。

    原缺陷链路：切换条挂在 ``WorkbenchCard`` 内容区 → 卡片是
    ``QScrollArea`` + ``widgetResizable(True)`` 的**整卡纵向滚动** → 判定原因
    （``reason`` / 三列链路 / 散行 OCR）一长，切换按钮被推到折叠线以下。
    本用例直接锁住"切换条不在卡片滚动内容内"这一结构事实。
    """
    workbench = _workbench(_evidence_result())
    card_content = workbench.card.scroll.widget()
    assert card_content is not None
    assert card_content.findChild(QFrame, "imageSwitcher") is None
    assert workbench.image_viewer.findChild(QFrame, "imageSwitcher") is not None


def test_switcher_buttons_visible_with_long_reason(qapp) -> None:
    """需求 1 可证伪回归锁：把判定原因撑到极长（但**不**处理事件队列）后，
    切换条按钮**仍未被隐藏**（原缺陷下切换按钮会被推到折叠线以下）。"""
    result = _evidence_result()
    result.reason = "品牌不一致；" * 400  # 撑爆卡片内容高度
    workbench = _workbench(result)
    workbench.resize(1400, 620)
    workbench.show()
    qapp.processEvents()
    assert workbench.image_viewer.btn_next_image.isHidden() is False
    assert workbench.image_viewer.btn_prev_image.isHidden() is False


def test_prev_next_buttons_step_images(qapp) -> None:
    """需求 1：``◀ 上一张`` / ``下一张 ▶`` 逐张前后翻，且首/尾自动禁用。"""
    workbench = _workbench(_evidence_result())
    viewer = workbench.image_viewer
    assert viewer.image_paths() == ["C:/img/1.jpg", "C:/img/2.jpg", "C:/img/3.jpg"]
    # 默认落在第一张 → 上一张不可用
    assert viewer.current_image_path() == "C:/img/1.jpg"
    assert viewer.btn_prev_image.isEnabled() is False
    assert viewer.btn_next_image.isEnabled() is True

    viewer.btn_next_image.click()
    assert viewer.current_image_path() == "C:/img/2.jpg"
    viewer.btn_next_image.click()
    assert viewer.current_image_path() == "C:/img/3.jpg"
    # 末张 → 下一张不可用
    assert viewer.btn_next_image.isEnabled() is False
    assert viewer.btn_prev_image.isEnabled() is True

    viewer.btn_prev_image.click()
    assert viewer.current_image_path() == "C:/img/2.jpg"


def test_step_image_syncs_card_current_image(qapp) -> None:
    """需求 1：切换条翻页必须**同步右侧证据卡**（否则"图切了、右边 OCR 没变"）。

    ⚠️ 断言用 ``current_image_path()``（切换条选中项）而非 ``current_path()``：
    本用例的图片路径是**虚构的**（``C:/img/*.jpg`` 不存在），``load()`` 会走
    失败分支清掉 ``_current_path`` —— 这不是本用例要锁的行为。
    """
    workbench = _workbench(_evidence_result())
    workbench.image_viewer.btn_next_image.click()
    assert workbench.card.current_image_path() == "C:/img/2.jpg"
    assert workbench.image_viewer.current_image_path() == "C:/img/2.jpg"


# ══════════════════════════════════════════════════════════════════
#  需求 2：命中图按钮标红
# ══════════════════════════════════════════════════════════════════


def test_hit_image_paths_from_exact_match() -> None:
    """需求 2/3 判据：命中集取自 ``TokenMatch.images``（UI 不重算判定）。"""
    assert hit_image_paths(_evidence_result()) == {"C:/img/2.jpg"}


def test_hit_image_paths_from_none_marker() -> None:
    """需求 2/3 判据：「图内显式标注无品牌」也是命中证据（口径 v0.3.2）。"""
    result = _evidence_result()
    result.token_matches = [
        TokenMatch(
            field="品牌",
            declared="",
            mode=MATCH_EXACT,
            token="无品牌",
            images=[],
            none_marker="无品牌",
            none_marker_image=3,
        )
    ]
    assert hit_image_paths(result) == {"C:/img/3.jpg"}


def test_hit_image_paths_ignores_unknown_seq() -> None:
    """需求 2/3：引擎回吐的图号找不到对应文件时**忽略**（不臆造路径）。"""
    result = _evidence_result()
    result.token_matches = [
        TokenMatch(field="品牌", declared="baori", mode=MATCH_EXACT, token="baori", images=[99])
    ]
    assert hit_image_paths(result) == set()


def test_hit_image_paths_ignores_non_hit_mode() -> None:
    """需求 2/3：``MATCH_NONE``（未命中）即便带图号也**不得**标红。"""
    result = _evidence_result()
    result.token_matches = [
        TokenMatch(field="品牌", declared="baori", mode=MATCH_NONE, token="", images=[2])
    ]
    assert hit_image_paths(result) == set()


def test_fuzzy_hit_also_counts() -> None:
    """需求 2/3：``FUZZY``（容错命中）同样算命中 → 标红 + 红星。"""
    result = _evidence_result()
    result.token_matches = [
        TokenMatch(field="型号", declared="A7A01G", mode=MATCH_FUZZY, token="A7A0IG", images=[1])
    ]
    assert hit_image_paths(result) == {"C:/img/1.jpg"}


def test_hit_button_uses_danger_color_others_use_theme_color(qapp) -> None:
    """需求 2：命中按钮为红色、未命中为主题色（四态各验一次）。"""
    workbench = _workbench(_evidence_result())
    buttons = workbench.image_viewer._image_buttons  # noqa: SLF001
    hit_qss = buttons["C:/img/2.jpg"].styleSheet()
    miss_qss = buttons["C:/img/1.jpg"].styleSheet()
    assert Palette.DANGER in hit_qss and Palette.ACCENT not in hit_qss
    assert Palette.ACCENT in miss_qss and Palette.DANGER not in miss_qss
    # 属性位（供 QSS 主题化 / 测试查询）
    assert buttons["C:/img/2.jpg"].property("evidenceHit") is True
    assert buttons["C:/img/1.jpg"].property("evidenceHit") is False


def test_switch_button_qss_four_states() -> None:
    """需求 2：纯函数四态配色（红/蓝 × 选中/未选中）逐一验明。"""
    assert Palette.DANGER in _switch_button_qss(hit=True, active=True)
    assert "#FFFFFF" in _switch_button_qss(hit=True, active=True)  # 选中 → 白字
    hit_idle = _switch_button_qss(hit=True, active=False)
    assert Palette.DANGER in hit_idle and "#FFFFFF" in hit_idle  # 未选中 → 白底
    assert Palette.ACCENT in _switch_button_qss(hit=False, active=True)
    assert Palette.ACCENT in _switch_button_qss(hit=False, active=False)


def test_switcher_marks_hit_in_tooltip(qapp) -> None:
    """需求 2 辅助线索：命中按钮的 tooltip 明确写「★ 本图命中申报要素」。"""
    workbench = _workbench(_evidence_result())
    buttons = workbench.image_viewer._image_buttons  # noqa: SLF001
    assert "★ 本图命中申报要素" in buttons["C:/img/2.jpg"].toolTip()
    assert "未命中" in buttons["C:/img/1.jpg"].toolTip()


# ══════════════════════════════════════════════════════════════════
#  需求 3：命中图右上角红色五角星
# ══════════════════════════════════════════════════════════════════


def test_star_lit_only_on_hit_image(qapp, tmp_path) -> None:
    """需求 3：命中图显示时点亮五角星；切到未命中图即熄灭。"""
    hit_png = _png(tmp_path / "hit.png")
    miss_png = _png(tmp_path / "miss.png")
    viewer = ImageViewer()
    viewer.set_images([(1, miss_png, False), (2, hit_png, True)])

    assert viewer.load(hit_png) is True
    viewer.set_current_image(hit_png)
    assert viewer.image_label.star() is True

    assert viewer.load(miss_png) is True
    viewer.set_current_image(miss_png)
    assert viewer.image_label.star() is False


def test_star_not_lit_when_nothing_selected(qapp, tmp_path) -> None:
    """需求 3：切换条未选中任何图时不点星（避免"星跑到占位文案上"）。"""
    png = _png(tmp_path / "b.png")
    viewer = ImageViewer()
    viewer.set_images([(1, png, True)])
    viewer.set_current_image("")
    assert viewer.image_label.star() is False


def test_star_cleared_on_placeholder_and_clear(qapp) -> None:
    """需求 3：``show_message`` / ``clear`` 必须清星（占位文案上不得顶星）。"""
    viewer = ImageViewer()
    viewer.set_images([(1, "C:/img/1.jpg", True)])
    viewer.show_message("🔵 缺图")
    assert viewer.image_label.star() is False
    viewer.clear()
    assert viewer.image_label.star() is False
    assert viewer.image_paths() == []


def test_star_paints_without_error(qapp, tmp_path) -> None:
    """需求 3：有星 / 无星两种情形下 ``paintEvent`` 均不得抛异常。"""
    from PySide6.QtGui import QPixmap

    stage = _ImageStage()
    stage.setPixmap(QPixmap(_png(tmp_path / "c.png")))
    stage.resize(200, 150)
    target = QPixmap(stage.size())
    for flag in (True, False):
        stage.set_star(flag)
        assert stage.star() is flag
        stage.render(target)  # 触发 paintEvent
    # 占位态（无 pixmap）不得画星，也不得崩
    stage.setPixmap(QPixmap())
    stage.set_star(True)
    stage.render(target)


def test_star_flag_defaults_false(qapp) -> None:
    """需求 3：新看一张未命中图时，星默认熄灭（不沿用上一张的状态）。"""
    viewer = ImageViewer()
    assert viewer.image_label.star() is False


def _png(path: Path) -> str:
    """生成一张纯色 PNG（供 QPixmap 加载）。"""
    from PySide6.QtGui import QColor, QPixmap

    pixmap = QPixmap(60, 40)
    pixmap.fill(QColor(180, 90, 40))
    assert pixmap.save(str(path), "PNG") is True
    return str(path)


# ══════════════════════════════════════════════════════════════════
#  需求 4：记录表标识 + 不覆盖原系统结果 + 复核后版本 + 保存即落盘
# ══════════════════════════════════════════════════════════════════


def test_record_table_marks_manual_review(qapp) -> None:
    """需求 4：人工重判后，记录表「复核」列出现标识（且红色加粗）。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    workbench = ReviewWorkbench(session)
    store = ReviewStore(session, _tmp_process())
    key = result.key

    # 改判前：无标识
    assert workbench.record_table.item(0, 3).text() == ""

    store.set_verdict(key, Verdict.FAIL, note="人工确认品牌不一致")
    workbench.reload()

    item = workbench.record_table.item(0, 3)
    assert item.text() == MANUAL_REVIEW_MARK
    assert item.foreground().color().name().upper() == Palette.DANGER.upper()
    assert item.font().bold() is True
    assert "原系统判定" in item.toolTip()
    assert VERDICT_TEXT[Verdict.PASS] in item.toolTip()
    assert "人工确认品牌不一致" in item.toolTip()


def test_review_mark_is_single_source() -> None:
    """需求 4：记录表第 4 列与导出追加列**同源**（``manual_review_mark`` 单一出口）。

    口径重复实现是 v0.3.6 已裁定要杜绝的（同一段文案只允许一个出口），
    故此处断言"导出层渲染的就是模型层那个方法"。
    """
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    ReviewStore(session, _tmp_process()).set_verdict(result.key, Verdict.FAIL)
    assert result.manual_review_mark() == MANUAL_REVIEW_MARK
    assert MANUAL_REVIEW_MARK in _reviewed_row(result)
    assert result.original_verdict_text() in _reviewed_row(result)


def test_original_verdict_survives_second_rejudge() -> None:
    """需求 4 关键锁：**二次改判不得改写原系统判定**。

    ``original_verdict`` 只应在**首次**改判时落定；否则它会退化成
    "上一次的重判值"，"系统判成什么"这一可追溯信息就永久丢失。
    """
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    store = ReviewStore(session, _tmp_process())
    assert result.verdict is Verdict.PASS
    assert result.original_verdict is None

    store.set_verdict(result.key, Verdict.FAIL, note="第一次")
    assert result.original_verdict is Verdict.PASS
    assert result.original_verdict_text() == VERDICT_TEXT[Verdict.PASS]

    store.set_verdict(result.key, Verdict.NO_MARK, note="第二次")
    assert result.original_verdict is Verdict.PASS, "二次改判不得污染系统判定"
    assert result.original_verdict_text() == VERDICT_TEXT[Verdict.PASS]
    assert result.verdict is Verdict.NO_MARK
    assert store.override_count() == 1  # 幂等：同 key 覆盖


def test_original_verdict_recorded_in_round_json(tmp_path) -> None:
    """需求 4：留痕 JSON 里记下每条的原系统判定（便于回溯）。"""
    import json

    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    process = _tmp_process()
    store = ReviewStore(session, process)
    store.set_verdict(result.key, Verdict.FAIL)
    payload = json.loads(store.save_round().read_text(encoding="utf-8"))
    rec = payload["records"][0]
    assert rec["original_verdict"] == Verdict.PASS.value
    assert rec["original_verdict_text"] == VERDICT_TEXT[Verdict.PASS]
    assert rec["verdict"] == Verdict.FAIL.value


def test_persist_writes_four_artifacts(temp_project) -> None:
    """需求 4「保存复核即落盘」：一次 ``persist`` 落 4 个文件，全部存在。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["root"])
    store.set_verdict(result.key, Verdict.FAIL, note="落盘验证")

    paths = store.persist(exporter)
    assert set(paths) == {"round", "reviewed_json", "summary_reviewed", "review_reviewed"}
    for name, path in paths.items():
        assert path.exists(), f"{name} 未落盘：{path}"
    assert paths["summary_reviewed"].name.endswith(f"{REVIEWED_NAME_SUFFIX}.xlsx")
    assert paths["review_reviewed"].name.endswith(f"{REVIEWED_NAME_SUFFIX}.csv")
    assert paths["round"].name.startswith("review_round_")


def test_persist_does_not_touch_original_artifacts(temp_project) -> None:
    """**红线锁**：复核落盘前后，系统原始三产物**逐字节不变**。

    原缺陷风险：复核后重导出直接覆盖 ``校验汇总表_{票号}.xlsx`` ——
    复核人再也无法对照"系统判成什么"。
    """
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    ticket = result.record.ticket_no

    # 1) 先产系统原始三产物（模拟跑批完成）
    original = exporter.export_all(session.results(), ticket)
    before = {name: path.read_bytes() for name, path in original.items()}

    # 2) 人工重判 + 保存即落盘
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["root"])
    store.set_verdict(result.key, Verdict.FAIL, note="人工改判")
    persisted = store.persist(exporter)

    # 3) 原始三产物必须一字未动
    for name, path in original.items():
        assert path.read_bytes() == before[name], f"原始产物被覆盖：{name}"
    # 4) 复核后版本与原始版本**并列存在**（不同文件名）
    assert persisted["summary_reviewed"] != original["summary"]
    assert persisted["review_reviewed"] != original["review"]


def test_reviewed_summary_appends_exactly_two_columns(temp_project) -> None:
    """需求 4：复核后汇总表 = 原 13 列 + 「人工复核 / 原系统判定」2 列（顺序冻结）。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["root"])
    store.set_verdict(result.key, Verdict.FAIL)
    paths = store.persist(exporter)

    headers = _xlsx_headers(paths["summary_reviewed"])
    assert headers[:COLUMN_COUNT] == list(COLUMNS), "原 13 列顺序不得变动"
    assert headers[COLUMN_COUNT:] == list(REVIEWED_EXTRA_COLUMNS)
    row = _xlsx_first_row(paths["summary_reviewed"])
    assert row[COLUMN_COUNT] == MANUAL_REVIEW_MARK
    assert row[COLUMN_COUNT + 1] == VERDICT_TEXT[Verdict.PASS]


def test_original_summary_still_13_columns(temp_project) -> None:
    """**13 列冻结红线**：系统原始汇总表恒为 13 列（复核列只进复核后版本）。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    original = exporter.export_all(session.results(), result.record.ticket_no)
    ReviewStore(session, temp_project["process"]).set_verdict(result.key, Verdict.FAIL)
    exporter.export_reviewed_all(session.results(), result.record.ticket_no)
    assert len(_xlsx_headers(original["summary"])) == COLUMN_COUNT


def test_reviewed_json_keeps_original_verdict_field(temp_project) -> None:
    """需求 4：复核后 JSON 每条都带 ``original_verdict`` / ``reviewed_at``。"""
    import json

    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["root"])
    store.set_verdict(result.key, Verdict.FAIL, note="JSON 留痕")
    paths = store.persist(exporter)

    payload = json.loads(paths["reviewed_json"].read_text(encoding="utf-8"))
    assert payload["manually_reviewed"] is True
    entry = payload["results"][0]
    assert entry["original_verdict"] == Verdict.PASS.value
    assert entry["original_verdict_text"] == VERDICT_TEXT[Verdict.PASS]
    assert entry["reviewed_at"], "复核时间必须留痕"
    assert entry["reviewer_note"] == "JSON 留痕"


def test_record_table_uses_mark_before_export(temp_project, qapp) -> None:
    """需求 4 端到端：改判 → 记录表出标识 → 复核后版本同步带上标识。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    exporter = ResultExporter(
        result_dir=temp_project["result"], process_dir=temp_project["process"]
    )
    workbench = ReviewWorkbench(session)
    store = ReviewStore(session, temp_project["process"], allowed_root=temp_project["root"])
    store.set_verdict(result.key, Verdict.FAIL)
    workbench.reload()
    assert workbench.record_table.item(0, 3).text() == MANUAL_REVIEW_MARK

    paths = store.persist(exporter)
    row = _xlsx_first_row(paths["summary_reviewed"])
    assert row[COLUMN_COUNT] == workbench.record_table.item(0, 3).text()


def test_reset_clears_store_and_row_marks(qapp) -> None:
    """需求 4：新一轮跑批重置后，不得留"幽灵改判"标识。"""
    from app.review_store import ReviewStore

    result = _evidence_result()
    session = _session(result)
    store = ReviewStore(session, _tmp_process())
    store.set_verdict(result.key, Verdict.FAIL)
    assert store.has_overrides() is True

    store.reset()
    assert store.has_overrides() is False
    assert store.override_count() == 0


# ══════════════════════════════════════════════════════════════════
#  构建期约束：切换条控件仍是按钮（防被换成不可点的 QLabel 回归）
# ══════════════════════════════════════════════════════════════════


def test_switcher_items_are_clickable_buttons(qapp) -> None:
    """需求 1：切换条上的图号项必须是可点按钮（左右翻页同理）。"""
    workbench = _workbench(_evidence_result())
    viewer = workbench.image_viewer
    buttons = list(viewer._image_buttons.values())  # noqa: SLF001
    assert len(buttons) == 3
    assert all(isinstance(btn, QPushButton) for btn in buttons)
    assert all(btn.isEnabled() for btn in buttons)
    assert isinstance(viewer.btn_prev_image, QPushButton)
    assert isinstance(viewer.btn_next_image, QPushButton)
    # 左/右按钮的点击方向不得写反（-1 上一张 / +1 下一张）
    assert viewer.btn_prev_image.toolTip().startswith("切换到上一张")
    assert viewer.btn_next_image.toolTip().startswith("切换到下一张")


def test_switcher_ignores_blank_paths(qapp) -> None:
    """需求 1 健壮性：空路径项不得进切换条（否则出现点了没反应的死按钮）。"""
    viewer = ImageViewer()
    viewer.set_images([(1, "C:/img/1.jpg", False), (2, "", False), (3, "   ", True)])
    assert viewer.image_paths() == ["C:/img/1.jpg"]


def test_empty_switcher_shows_hint(qapp) -> None:
    """需求 1：无图时切换条显示占位提示，而不是留一排死按钮。"""
    viewer = ImageViewer()
    viewer.set_images([])
    assert viewer.image_paths() == []
    assert viewer._switch_hint is not None  # noqa: SLF001
    assert "无可预览图片" in viewer._switch_hint.text()  # noqa: SLF001


def test_switcher_has_no_ghost_buttons_after_rebuild(qapp) -> None:
    """**v0.3.7 缺陷锁**：连续 ``set_images()`` 不得留下上一轮的按钮**残影**。

    原缺陷：``_clear_thumb_buttons`` 只调 ``deleteLater()`` —— 它是**异步**的
    （下一轮事件循环才析构），而 ``takeAt(0)`` 只把控件从**布局**摘掉；控件此时
    仍是 ``thumbs_area`` 的子控件，于是**继续按旧坐标渲染**。

    实测表现：先装 4 张（图1 图2 图4 图6）再装 2 张 → 屏幕上同时看到
    「图1 图2 图4」和残留的「图4 图6」，即 **图4 出现两次**。

    本用例**刻意不 processEvents**（残影只在下一轮事件循环才会消失，跑事件循环
    会把缺陷掩盖掉）→ 断言「``thumbs_area`` 的子控件集合 ≡ 布局里的项集合」。
    ⚠️ 不能用 ``isHidden()`` 判可见：它只反映**显式隐藏标记**，父级尚未显示时
    当前按钮同样是 ``False``（实测踩坑），据此会把"新按钮"误判成残影。
    """
    viewer = ImageViewer()
    viewer.set_images(
        [
            (1, "C:/a.jpg", False),
            (2, "C:/b.jpg", True),
            (4, "C:/c.jpg", True),
            (6, "C:/d.jpg", True),
        ]
    )
    assert viewer.thumbs_layout.count() == 4
    assert len(viewer.thumbs_area.findChildren(QPushButton)) == 4

    viewer.set_images([(1, "C:/a.jpg", False), (2, "C:/b.jpg", True)])

    assert viewer.thumbs_layout.count() == 2, "布局里只应剩 2 个"
    assert len(viewer._image_buttons) == 2  # noqa: SLF001
    kids = viewer.thumbs_area.findChildren(QPushButton)
    in_layout = [
        viewer.thumbs_layout.itemAt(i).widget()
        for i in range(viewer.thumbs_layout.count())
    ]
    assert len(kids) == 2, f"thumbs_area 下残留旧按钮：{[b.text() for b in kids]}"
    assert {id(b) for b in kids} == {id(b) for b in in_layout}
    assert len(viewer.thumbs_area.findChildren(QPushButton)) == 2


def test_card_flow_labels_cleared_without_ghosts(qapp) -> None:
    """**v0.3.7 缺陷锁（同一根因的另一处）**：``WorkbenchCard._clear_layout``
    切记录时不得留下上一条的散行标签**残影**。

    与 :func:`test_switcher_has_no_ghost_buttons_after_rebuild` 同源：只
    ``takeAt`` + ``deleteLater`` 的话，旧标签会作为残影继续渲染（异步析构）。
    断言口径：**散行区父控件下可见的标签数 == 布局里的项数**。
    """
    from ui.widgets.workbench_card import WorkbenchCard

    card = WorkbenchCard()
    many = _evidence_result()
    many.evidence_images[0].ocr = _ocr("L1\nL2\nL3\nL4")
    card.load_result(many)
    assert card.line_flow.count() == 4

    few = _evidence_result()
    few.evidence_images[0].ocr = _ocr("ONLY-ONE")
    card.load_result(few)
    assert card.line_flow.count() == 1

    in_layout = {
        id(card.line_flow.itemAt(i).widget()) for i in range(card.line_flow.count())
    }
    visible = {id(lb) for lb in card.line_area.findChildren(QLabel) if not lb.isHidden()}
    assert visible == in_layout, "存在不在布局里却仍可见的残影标签"


# ══════════════════════════════════════════════════════════════════
#  辅助
# ══════════════════════════════════════════════════════════════════


def _tmp_process() -> Path:
    """返回一个临时过程产出目录（供不落盘的 store 用）。"""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="v037_proc_"))


def _reviewed_row(result: CheckResult) -> str:
    """取复核后版本追加列的拼串（用于断言同源）。"""
    return "".join(ResultExporter._reviewed_extra(result))  # noqa: SLF001


def _xlsx_headers(path: Path) -> list[str]:
    """读取 xlsx 首行表头。"""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    sheet = book.active
    headers = [str(cell.value or "") for cell in next(sheet.iter_rows(max_row=1))]
    book.close()
    return headers


def _xlsx_first_row(path: Path) -> list[str]:
    """读取 xlsx 首个数据行。"""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    sheet = book.active
    rows = sheet.iter_rows(min_row=2, max_row=2)
    values = [str(cell.value if cell.value is not None else "") for cell in next(rows)]
    book.close()
    return values


def test_constants_export_has_manual_review_mark() -> None:
    """需求 4：``MANUAL_REVIEW_MARK`` 由常量模块给出（口径单一来源）。"""
    from core import constants

    assert constants.MANUAL_REVIEW_MARK == MANUAL_REVIEW_MARK


def test_switch_button_property_is_bool(qapp) -> None:
    """需求 2：``evidenceCurrent`` / ``evidenceHit`` 为 bool（Qt 属性查询不返回 None）。

    注意两者**互相独立**：默认选中图 1（未命中）→ 图 1 是 current、图 2 是 hit。
    """
    workbench = _workbench(_evidence_result())
    buttons = workbench.image_viewer._image_buttons  # noqa: SLF001
    selected = buttons["C:/img/1.jpg"]
    hit = buttons["C:/img/2.jpg"]
    assert selected.property("evidenceCurrent") is True
    assert selected.property("evidenceHit") is False
    assert hit.property("evidenceCurrent") is False
    assert hit.property("evidenceHit") is True
    assert None not in (
        selected.property("evidenceCurrent"),
        selected.property("evidenceHit"),
    )


def test_star_size_reasonable() -> None:
    """需求 3：五角星绘制区尺寸合理（不得大到遮住图片主体、不得小到看不见）。"""
    assert 16 <= _ImageStage._STAR_SIZE <= 48  # noqa: SLF001
    assert 4 <= _ImageStage._STAR_PAD <= 24  # noqa: SLF001


def test_switcher_hint_cleared_when_images_return(qapp) -> None:
    """需求 1：占位提示在重新装图后必须清掉（不得与按钮并存）。"""
    viewer = ImageViewer()
    viewer.set_images([])
    viewer.set_images([(1, "C:/img/1.jpg", False)])
    assert viewer._switch_hint is None  # noqa: SLF001
    assert viewer.image_paths() == ["C:/img/1.jpg"]


def test_hit_label_text_reflects_star(qapp, tmp_path) -> None:
    """需求 3：切换条上的「★ 本图命中申报要素」与五角星同开同关。"""
    png = _png(tmp_path / "d.png")
    viewer = ImageViewer()
    viewer.set_images([(1, png, True)])
    viewer.load(png)
    viewer.set_current_image(png)
    assert viewer.hit_label.text() == "★ 本图命中申报要素"
    viewer.set_images([(1, png, False)])
    viewer.set_current_image("")
    assert viewer.hit_label.text() == ""


def test_switcher_cursor_is_pointing_hand(qapp) -> None:
    """需求 1：切换按钮用「手型」光标（可点提示）。"""
    workbench = _workbench(_evidence_result())
    viewer = workbench.image_viewer
    assert viewer.btn_prev_image.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert viewer.btn_next_image.cursor().shape() == Qt.CursorShape.PointingHandCursor
    for btn in viewer._image_buttons.values():  # noqa: SLF001
        assert btn.cursor().shape() == Qt.CursorShape.PointingHandCursor
