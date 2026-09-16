"""人工复核工作台 D 类回归 + v0.3.0 工作台重构验收。

在 ``QT_QPA_PLATFORM=offscreen`` 下用**夹具构造的 CheckResult**（不跑真实跑批）
验证纯 UI 行为：

  * **9.1 拖动平移**：``setWidgetResizable(False)``；放大超出视口后
    ``horizontalScrollBar().maximum() > 0``（原缺陷下为 0）；左键拖拽改变滚动值；
    抓手光标**仅在溢出时**出现；``Ctrl + 滚轮`` 缩放保留。
  * **9.2 只显当前图**：带 3 张图的记录，切换后散行区**只含当前图**文本
    （可证伪回归锁：原缺陷下会拼接全部 3 张）。
  * **需求 5 判定链路三列**：``要素 | 申报值 | 判定值``，「判定值」取引擎回吐的
    ``TokenMatch``（EXACT/FUZZY/NONE 三分支 + 旧链路回退）。
  * **需求 7 全散行**：废弃 KV 表格，当前图 OCR 全部行走散行标签卡。
  * **需求 8 原始 OCR 弹窗**：单例、非模态、**不遮挡图片区**（``overlap_with`` 断言）。
  * **需求 2 图片旋转**：左旋 / 右旋 90°、四步回原位、换图复位、旋转后尺寸基准跟随。
  * **需求 3 + 4 记录区**：表格三列、5 个状态标签（带数量）、三字段模糊搜索、
    选中行同步中/右区。
  * **P2-2 票号纠正入口**：只读标签旁的「改」按钮弹 ``QInputDialog``，改完刷新；
    取消 / 留空不改；运行期置灰。
  * **v0.3.1 现场反馈修正**：判定链路表**不错位/不覆盖**（表头左对齐、关闭滚动条、
    表高按内容自适应、前两列显式固定宽）；OCR 弹窗**右侧垂直居中、不置顶**
    （定位纯函数 ``OcrTextDialog.place`` 另见 ``tests/test_ocr_dialog_position.py``）。

无 PySide6 环境整体 skip。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="无 PySide6，跳过 UI 用例")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent, QPixmap, QWheelEvent  # noqa: E402

from core.constants import VERDICT_FAIL  # noqa: E402
from core.models import (  # noqa: E402
    CheckResult,
    DeclarationRecord,
    DifferenceDetail,
    ImageEvidence,
    OcrText,
    Verdict,
)
from core.token_matcher import MATCH_EXACT, MATCH_FUZZY, MATCH_NONE, TokenMatch  # noqa: E402


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


def _make_png(path, width: int, height: int):
    """生成一张纯色 PNG 到磁盘（供 QPixmap 加载）。"""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(180, 90, 40))
    assert pixmap.save(str(path), "PNG") is True
    return str(path)


def _ocr(text: str, *, kv=None, confidence: float = 0.9, line_scores=None) -> OcrText:
    """构造一个 OcrText（简化夹具）。"""
    return OcrText(
        text_raw=text,
        confidence=confidence,
        kv=dict(kv or {}),
        line_scores=list(line_scores or []),
    )


def _three_image_result() -> CheckResult:
    """构造一条带 3 张图的记录（每张图 OCR 文本互不相同，便于可证伪断言）。"""
    record = DeclarationRecord(
        ticket_no="SA26090215",
        part_no="N1",
        order_no="O1",
        decl_brand="baori",
        decl_model="A7A01G",
    )
    images = [
        ImageEvidence(
            image_path="C:/img/1.jpg",
            seq=1,
            exists=True,
            ocr=_ocr("AAA-BRAND-TEXT", kv={"Brand": "Daewoo"}, line_scores=[0.92]),
        ),
        ImageEvidence(
            image_path="C:/img/2.jpg",
            seq=2,
            exists=True,
            ocr=_ocr("BBB-MODEL-TEXT", kv={"Model": "HS-8AA"}),
        ),
        ImageEvidence(
            image_path="C:/img/3.jpg",
            seq=3,
            exists=True,
            ocr=_ocr("CCC-RESIDUE-TEXT"),
        ),
    ]
    return CheckResult(
        key=record.key(),
        record=record,
        verdict=Verdict.FAIL,
        detected_brand="Daewoo",
        detected_model="HS-8AA",
        differences=[
            DifferenceDetail(
                field="品牌",
                declared_value="baori",
                detected_value="Daewoo",
                char_diffs=["b→D", "a→a", "o→e"],
            )
        ],
        reason="品牌不一致",
        evidence_images=images,
    )


def _card(qapp):
    """构造一个独立的 WorkbenchCard（无需 MainWindow）。"""
    from ui.widgets.workbench_card import WorkbenchCard

    return WorkbenchCard()


# ══════════════════════════════════════════════════════════════════
#  9.1 拖动平移
# ══════════════════════════════════════════════════════════════════


def _viewer(qapp, width: int, height: int):
    from ui.widgets.image_viewer import ImageViewer

    viewer = ImageViewer()
    viewer.resize(width, height)
    viewer.show()
    qapp.processEvents()
    return viewer


def test_widget_resizable_disabled(qapp) -> None:
    """9.1：滚动区关闭 WidgetResizable（否则 label 被强制拉伸，滚动条不出现）。"""
    viewer = _viewer(qapp, 400, 320)
    assert viewer.scroll.widgetResizable() is False


def test_zoom_overflow_shows_horizontal_scrollbar(qapp, tmp_path) -> None:
    """9.1 可证伪回归锁：放大超出视口后水平滚动条 maximum() > 0（原缺陷为 0）。"""
    viewer = _viewer(qapp, 200, 160)
    big = _make_png(tmp_path / "big.png", 800, 800)
    assert viewer.load(big) is True
    viewer.reset_zoom()  # 1:1 → 800×800 远大于视口
    qapp.processEvents()
    assert viewer.is_pannable() is True
    assert viewer.scroll.horizontalScrollBar().maximum() > 0
    assert viewer.scroll.verticalScrollBar().maximum() > 0


def test_no_hand_cursor_when_not_overflowing(qapp, tmp_path) -> None:
    """9.1：图片未超出视口时不出现抓手光标（避免用户困惑）。"""
    viewer = _viewer(qapp, 600, 500)
    small = _make_png(tmp_path / "small.png", 100, 80)
    assert viewer.load(small) is True
    viewer.reset_zoom()
    qapp.processEvents()
    assert viewer.is_pannable() is False
    assert viewer.image_label.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_hand_cursor_when_overflowing(qapp, tmp_path) -> None:
    """9.1：图片放大超出视口时出现「张开的手」抓手光标。"""
    viewer = _viewer(qapp, 200, 160)
    big = _make_png(tmp_path / "big2.png", 900, 900)
    assert viewer.load(big) is True
    viewer.reset_zoom()
    qapp.processEvents()
    assert viewer.image_label.cursor().shape() == Qt.CursorShape.OpenHandCursor


def _mouse_event(event_type, x: int, y: int, button, buttons) -> QMouseEvent:
    """构造一个本地/全局坐标相同的鼠标事件。"""
    pos = QPointF(float(x), float(y))
    return QMouseEvent(
        event_type, pos, pos, button, buttons, Qt.KeyboardModifier.NoModifier
    )


def test_left_drag_pans_horizontal_scrollbar(qapp, tmp_path) -> None:
    """9.1：左键拖拽直接平移滚动条（松手复位光标）。"""
    viewer = _viewer(qapp, 200, 160)
    big = _make_png(tmp_path / "big3.png", 900, 900)
    assert viewer.load(big) is True
    viewer.reset_zoom()
    qapp.processEvents()
    hbar = viewer.scroll.horizontalScrollBar()
    hbar.setValue(hbar.maximum() // 2)
    before = hbar.value()

    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 10, 10, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton
    )
    viewer.mousePressEvent(press)
    assert viewer.image_label.cursor().shape() == Qt.CursorShape.ClosedHandCursor

    move = _mouse_event(
        QEvent.Type.MouseMove, 0, 0, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton
    )
    viewer.mouseMoveEvent(move)
    assert hbar.value() == before + 10  # 往左拖 → 内容右移 → 滚动值 +10

    release = _mouse_event(
        QEvent.Type.MouseButtonRelease, 0, 0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    )
    viewer.mouseReleaseEvent(release)
    assert viewer.image_label.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_drag_ignored_when_not_overflowing(qapp, tmp_path) -> None:
    """9.1：图片未溢出时按下左键不进入拖拽态（不误触发平移）。"""
    viewer = _viewer(qapp, 600, 500)
    small = _make_png(tmp_path / "small2.png", 100, 80)
    assert viewer.load(small) is True
    viewer.reset_zoom()
    qapp.processEvents()
    press = _mouse_event(
        QEvent.Type.MouseButtonPress, 5, 5, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton
    )
    viewer.mousePressEvent(press)
    assert viewer._dragging is False  # noqa: SLF001


def test_ctrl_wheel_zoom_preserved(qapp, tmp_path) -> None:
    """9.1：Ctrl + 滚轮缩放行为保留。"""
    viewer = _viewer(qapp, 300, 260)
    big = _make_png(tmp_path / "big4.png", 600, 600)
    assert viewer.load(big) is True
    before = viewer.current_scale()
    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    viewer.wheelEvent(event)
    assert viewer.current_scale() > before


# ══════════════════════════════════════════════════════════════════
#  9.2 切换图片只显示当前图的 OCR
# ══════════════════════════════════════════════════════════════════


def _flow_text(layout) -> str:
    """把 FlowLayout 中标签卡的文本拼成一段（供「只显当前图」类断言）。"""
    texts: list[str] = []
    for idx in range(layout.count()):
        item = layout.itemAt(idx)
        widget = item.widget() if item is not None else None
        if widget is not None and hasattr(widget, "text"):
            texts.append(widget.text())
    return "\n".join(texts)


def _card_text(card) -> str:
    """读取卡片散行区（``line_flow``）的全部文本。"""
    return _flow_text(card.line_flow)


def test_line_flow_only_current_image(qapp) -> None:
    """9.2 可证伪回归锁：默认只显第 1 张图 OCR，不含另两张文本。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = _card_text(card)
    assert "AAA-BRAND-TEXT" in text
    assert "BBB-MODEL-TEXT" not in text
    assert "CCC-RESIDUE-TEXT" not in text


def test_switch_image_updates_line_flow(qapp) -> None:
    """9.2：切到第 2 张图后，散行区只含第 2 张文本（不含第 1、3 张）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card.set_current_image("C:/img/2.jpg")
    text = _card_text(card)
    assert "BBB-MODEL-TEXT" in text
    assert "AAA-BRAND-TEXT" not in text
    assert "CCC-RESIDUE-TEXT" not in text


def test_default_current_image_is_first_existing(qapp) -> None:
    """9.2：默认当前图 = 第一条存在图。"""
    card = _card(qapp)
    result = _three_image_result()
    # 第 1 张不存在 → 默认应落到第 2 张（第一条存在）
    result.evidence_images[0].exists = False
    card.load_result(result)
    assert card.current_image_path() == "C:/img/2.jpg"


def test_current_image_button_highlighted(qapp) -> None:
    """9.2：图号按钮区保留为切换器，当前选中态高亮。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card.set_current_image("C:/img/3.jpg")
    assert card._seq_buttons["C:/img/3.jpg"].property("evidenceCurrent") is True  # noqa: SLF001
    assert card._seq_buttons["C:/img/1.jpg"].property("evidenceCurrent") is False  # noqa: SLF001


def test_workbench_evidence_selected_syncs_card(qapp) -> None:
    """9.2：工作台切换图片时同步卡片当前图（右侧跟着变）。"""
    from app.session import AppSession
    from ui.widgets.review_workbench import ReviewWorkbench

    session = AppSession()
    result = _three_image_result()
    result.verdict = Verdict.NO_MARK
    session.replace([result], ticket_no="SA26090215")
    workbench = ReviewWorkbench(session)
    workbench._on_evidence_selected("C:/img/2.jpg")  # noqa: SLF001
    assert "BBB-MODEL-TEXT" in _card_text(workbench.card)


# ══════════════════════════════════════════════════════════════════
#  需求 5：判定链路三列（要素 / 申报值 / 判定值）
# ══════════════════════════════════════════════════════════════════


def _result_with_matches(
    brand_match: TokenMatch | None = None,
    model_match: TokenMatch | None = None,
) -> CheckResult:
    """在基础夹具上挂 ``token_matches``（模拟引擎回吐）。"""
    result = _three_image_result()
    matches: list[TokenMatch] = []
    if brand_match is not None:
        matches.append(brand_match)
    if model_match is not None:
        matches.append(model_match)
    result.token_matches = matches
    return result


def test_chain_table_has_three_columns_two_rows(qapp) -> None:
    """需求 5：判定链路为「要素 | 申报值 | 判定值」3 列 × 2 行（品牌 / 型号）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    assert card.chain_table.columnCount() == 3
    assert card.chain_table.rowCount() == 2
    headers = [
        card.chain_table.horizontalHeaderItem(col).text()
        for col in range(card.chain_table.columnCount())
    ]
    assert headers == ["要素", "申报值", "判定值"]
    assert card.chain_table.item(0, 0).text() == "品牌"
    assert card.chain_table.item(1, 0).text() == "型号"


# ── 需求 5 修正：表格错位 / 覆盖（v0.3.1 现场反馈）──


def test_chain_header_is_left_aligned_like_cells(qapp) -> None:
    """现场反馈修正：表头**左对齐**，与单元格对齐方式一致 → 不再视觉错位。

    ``QHeaderView`` 默认是**居中**对齐，而 ``QTableWidgetItem`` 默认左对齐；
    两者混用会让"申报值 / 判定值"的表头文字与列内容看起来错开。
    """
    card = _card(qapp)
    card.load_result(_three_image_result())
    header = card.chain_table.horizontalHeader()
    alignment = header.defaultAlignment()
    assert bool(alignment & Qt.AlignmentFlag.AlignLeft)
    assert not bool(alignment & Qt.AlignmentFlag.AlignHCenter)


def test_chain_table_has_no_scrollbars(qapp) -> None:
    """现场反馈修正：判定链路表**关闭双滚动条**（高度按内容自适应）。

    滚动条一旦出现会同时 ① 挤掉右侧列宽（表头与单元格几何不同步）
    ② 把「型号」行推出视口，与下方「判定原因」挤在一起 —— 即现场看到的
    "错位覆盖"。
    """
    card = _card(qapp)
    card.load_result(_three_image_result())
    assert (
        card.chain_table.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert (
        card.chain_table.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )


def test_chain_table_height_fits_content_without_gap(qapp) -> None:
    """现场反馈修正：表高 = **表头实际高** + 各行高 + 边框 → 不裁行、不留空白。

    写死高度时表头一变高就会冒出滚动条裁掉「型号」行；
    不做约束时 ``QTableWidget`` 默认 ``sizeHint`` 高 192px 又在表下留一大片空白。
    """
    card = _card(qapp)
    card.show()
    card.load_result(_three_image_result())
    qapp.processEvents()

    table = card.chain_table
    expected = table.horizontalHeader().height() + 2 * table.frameWidth() + 2
    expected += sum(table.rowHeight(row) for row in range(table.rowCount()))
    assert table.height() == expected
    # 两行都在视口内（型号行不得被推出）
    viewport_h = table.viewport().height()
    assert viewport_h >= sum(
        table.rowHeight(row) for row in range(table.rowCount())
    )


def test_chain_columns_use_explicit_widths(qapp) -> None:
    """现场反馈修正：前两列用**显式固定宽**，第三列拉伸 → 表头与单元格几何一致。

    ``ResizeToContents`` + ``Stretch`` 混用时列宽会随内容与 splitter 拖动反复重算，
    表头与单元格可能不同步。
    """
    from PySide6.QtWidgets import QHeaderView

    from ui.widgets.workbench_card import _CHAIN_COL_DECLARED, _CHAIN_COL_ELEMENT

    card = _card(qapp)
    card.load_result(_three_image_result())
    header = card.chain_table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch
    assert card.chain_table.columnWidth(0) == _CHAIN_COL_ELEMENT
    assert card.chain_table.columnWidth(1) == _CHAIN_COL_DECLARED


# ── 需求 5 修正之二：卡片内容被压缩 → 链路框「判定原因」压住「型号」行 ──
#
# 现场第二张截图暴露的**真根因**不在表格自身，而在父容器：
#   ① 卡片内容高 > 窗格高 → QVBoxLayout 把子控件压到「最小尺寸之下」
#      （实测链路框只剩 190px，其 minimumSizeHint 是 194px）；
#   ② 之所以撑不高，是因为 `QLayout.addWidget()` 的显示是 **queued** 的 ——
#      刚 new 出来的散行标签在事件循环下一拍前仍 isHidden()，FlowLayout 的
#      sizeHint / heightForWidth 一律返回 0（实测 0 / 577 / 718，稳定后 886）。
# 修法：整卡套 QScrollArea + 按 heightForWidth 设内容最小高 + 新增流式子控件
# 显式 show()。以下 4 条为对应回归锁。


def _squeezed_card(qapp):
    """给一个**故意比内容矮**的卡片（复现"内容撑不下"的现场条件）。"""
    card = _card(qapp)
    card.resize(506, 360)
    card.show()
    card.load_result(_three_image_result())
    qapp.processEvents()
    return card


def test_chain_frame_is_never_squeezed_below_its_minimum(qapp) -> None:
    """可证伪回归锁：判定链路框**不得**被压到最小尺寸之下。

    原缺陷下实测 ``chainFrame.height() == 190 < minimumSizeHint().height() == 194``
    —— 少掉的 4px 正是「判定原因」与表格重叠的起点。
    """
    card = _squeezed_card(qapp)
    frame = card.chain_table.parentWidget()
    assert frame.height() >= frame.minimumSizeHint().height()


def test_chain_reason_does_not_overlap_table(qapp) -> None:
    """可证伪回归锁：三块纵向几何**严格不交叠**（表格 ⊂ 原因 ⊂ 结果）。"""
    card = _squeezed_card(qapp)
    table = card.chain_table.geometry()
    reason = card.chain_reason.geometry()
    verdict = card.chain_verdict.geometry()
    assert reason.top() >= table.bottom(), f"判定原因压住表格：{reason} vs {table}"
    assert verdict.top() >= reason.bottom()
    # 反向锁：表格两行都在自身视口内（未被滚动条推出）
    assert card.chain_table.viewport().height() >= sum(
        card.chain_table.rowHeight(r) for r in range(card.chain_table.rowCount())
    )


def test_card_content_takes_height_for_width(qapp) -> None:
    """内容最小高 = ``heightForWidth(视口宽)``，且重算**幂等不漂移**。

    原缺陷下 ``content`` 被钉在视口高（759），``minimumHeight`` 在
    0 / 577 / 718 之间乱跳（每拍算出的值都不同）→ 分配不足 → 压扁子控件。
    """
    card = _squeezed_card(qapp)
    content = card.scroll.widget()
    width = card.scroll.viewport().width()
    need = content.heightForWidth(width)
    assert need > 0
    assert content.minimumHeight() == need
    # 幂等：连算 3 次不得漂移（原缺陷下是 684→718→577 式的抖动）
    for _ in range(3):
        card._fit_content_height()  # noqa: SLF001
        assert content.minimumHeight() == need
    assert card.scroll.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_flow_labels_are_shown_synchronously_after_render(qapp) -> None:
    """**根因锁**：新增流式子控件被**当拍** show，几何查询不得返回 0。

    ``QLayout.addWidget()`` 内部用 queued ``_q_showIfNotHidden`` 显示子控件；
    若不显式 show，本用例（**刻意不 processEvents**）会看到标签仍 hidden、
    ``FlowLayout.heightForWidth() == 0``。
    """
    card = _card(qapp)
    card.show()
    card.load_result(_three_image_result())  # 刻意不 processEvents
    labels = [
        card.line_flow.itemAt(i).widget() for i in range(card.line_flow.count())
    ]
    labels = [lb for lb in labels if lb is not None]
    assert labels, "散行区应有标签"
    assert all(not lb.isHidden() for lb in labels), "新增标签必须当拍非隐藏"
    assert card.line_flow.heightForWidth(card.scroll.viewport().width()) > 0
    # 证据图号按钮同理（同一 queued 机制）
    assert card.evidence_buttons_row.count() > 0
    btn = card.evidence_buttons_row.itemAt(0).widget()
    assert btn is not None and not btn.isHidden()


def test_chain_declared_column_shows_declared_values(qapp) -> None:
    """需求 5：中间列取 ``record.decl_brand`` / ``decl_model``。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    assert card.chain_table.item(0, 1).text() == "baori"
    assert card.chain_table.item(1, 1).text() == "A7A01G"


def test_chain_declared_column_shows_dash_when_absent(qapp) -> None:
    """需求 5：申报值为空 → 显示「（无）」。"""
    card = _card(qapp)
    result = _three_image_result()
    result.record.decl_brand = ""
    card.load_result(result)
    assert card.chain_table.item(0, 1).text() == "（无）"


def test_chain_judge_exact_hit_shows_token_and_images(qapp) -> None:
    """需求 5 + 遗留 #3：EXACT 命中 → 显示命中 token 与**引擎回吐的真实图号**。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(
            field="品牌",
            declared="baori",
            mode=MATCH_EXACT,
            token="baori",
            images=[2],
            sample_line="baori E339609 AWM 20941",
        )
    )
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "✅" in text
    assert "完整分词" in text
    assert "baori" in text
    assert "图 2" in text


def test_chain_judge_fuzzy_hit_marks_correction(qapp) -> None:
    """需求 5：FUZZY 命中 → 必须标注「OCR 疑似误读，已纠正」（可追溯）。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(
            field="品牌",
            declared="baori",
            mode=MATCH_FUZZY,
            token="baori",
            images=[1],
            corrected_from="boori",
        )
    )
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "✅" in text
    assert "boori" in text
    assert "已纠正" in text


def test_chain_judge_none_with_detected_shows_fail(qapp) -> None:
    """需求 5：未命中但图内有同类标识 → ❌ 并列出图内实际值（两级分流之一）。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(field="品牌", declared="baori", mode=MATCH_NONE, note="未出现完整分词")
    )
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "❌" in text
    assert "Daewoo" in text


def test_chain_judge_none_without_detected_shows_no_mark(qapp) -> None:
    """需求 5：未命中且图内无同类标识 → ⚠️ 缺图内标识（两级分流之二，严禁 ❌）。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(field="品牌", declared="baori", mode=MATCH_NONE, note="未出现完整分词")
    )
    result.detected_brand = ""
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "⚠️" in text
    assert "❌" not in text


def test_chain_judge_voided_hit_explains_guard(qapp) -> None:
    """需求 5 + 不虚高：命中被字段名护栏作废 → 原样展示原因（不静默）。"""
    card = _card(qapp)
    note = "品牌：『创维』仅出现在字段名语境（如『创维物料编号』），不构成值证据，命中作废"
    result = _result_with_matches(
        TokenMatch(field="品牌", declared="创维", mode=MATCH_NONE, note=note)
    )
    result.detected_brand = ""
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "⚠️" in text
    assert "命中作废" in text


def test_chain_judge_declared_none_token(qapp) -> None:
    """需求 5：申报值为「无」→ 明确标注未参与匹配（不得显示为命中/失败）。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(field="品牌", declared="无", mode=MATCH_NONE, note="申报值为『无』")
    )
    card.load_result(result)
    text = card.chain_table.item(0, 2).text()
    assert "申报为无" in text
    assert "✅" not in text


def test_chain_judge_falls_back_to_legacy_detected(qapp) -> None:
    """需求 5 回退：无 ``token_matches``（旧结果集）→ 显示 detected_* 并标注旧链路。"""
    card = _card(qapp)
    card.load_result(_three_image_result())  # 该夹具未挂 token_matches
    text = card.chain_table.item(0, 2).text()
    assert "Daewoo" in text
    assert "旧链路" in text


def test_chain_judge_cell_tooltip_is_traceable(qapp) -> None:
    """需求 5 可追溯：判定值单元格 tooltip 含命中方式 / 原文行。"""
    card = _card(qapp)
    result = _result_with_matches(
        TokenMatch(
            field="品牌",
            declared="baori",
            mode=MATCH_EXACT,
            token="baori",
            images=[2],
            sample_line="baori E339609",
            note="品牌：图片中以完整分词命中『baori』（图 2）",
        )
    )
    card.load_result(result)
    tooltip = card.chain_table.item(0, 2).toolTip()
    assert "命中方式：EXACT" in tooltip
    assert "baori E339609" in tooltip


def test_chain_reason_section_lists_char_diffs(qapp) -> None:
    """判定原因：含差异字段与逐字符差异点。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_reason.text()
    assert "判定原因" in text
    assert "品牌" in text
    assert "b→D" in text


def test_chain_verdict_section_has_content(qapp) -> None:
    """判定结果：含四类判定字符串（口径逐字）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_verdict.text()
    assert "判定结果" in text
    assert VERDICT_FAIL in text


def test_chain_reason_falls_back_to_reason_text(qapp) -> None:
    """判定原因：无差异明细时回退到 ``result.reason``。"""
    card = _card(qapp)
    result = _three_image_result()
    result.differences = []
    result.reason = "申报缺失但图片明确有"
    card.load_result(result)
    assert "申报缺失但图片明确有" in card.chain_reason.text()


def test_no_legacy_brand_source_hint(qapp) -> None:
    """v0.3 遗留 #3 闭环：UI 侧「文本包含」近似推断函数已删除（改由引擎回吐）。"""
    card = _card(qapp)
    assert not hasattr(card, "_brand_source_hint")


# ══════════════════════════════════════════════════════════════════
#  需求 7 + 8：全散行文本 + 原始 OCR 弹窗
# ══════════════════════════════════════════════════════════════════


def test_no_kv_table_widget(qapp) -> None:
    """需求 7：KV 表格已删除（不再以键值对齐方式呈现）。"""
    card = _card(qapp)
    assert not hasattr(card, "kv_table")
    assert not hasattr(card, "_kv_rows")
    assert not hasattr(card, "_line_key")


def test_no_duplicate_text_widgets(qapp) -> None:
    """需求 8：重复的「当前图 OCR 文本」与内嵌折叠区均已删除，只留一个弹窗入口。"""
    card = _card(qapp)
    assert not hasattr(card, "evidence_view")
    assert not hasattr(card, "raw_toggle")
    assert not hasattr(card, "raw_view")
    assert card.btn_view_ocr.text() == "查看原始 OCR 文本"


def test_line_flow_keeps_kv_lines(qapp) -> None:
    """需求 7：散行区取 ``ocr.lines()`` **全量行**，不再剔除已成键值对的行。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[0].ocr = _ocr(
        "Brand:Daewoo\nModel:HS-8AA\nMADE IN CHINA",
        kv={"Brand": "Daewoo", "Model": "HS-8AA"},
    )
    card.load_result(result)
    assert card.line_flow.count() == 3
    texts = [card.line_flow.itemAt(i).widget().text() for i in range(3)]
    assert "Brand:Daewoo" in texts
    assert "Model:HS-8AA" in texts


def test_line_flow_empty_hint(qapp) -> None:
    """需求 7：当前图无识别文本 → 显示空态提示（而非空白）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[0].ocr = _ocr("")
    card.load_result(result)
    assert card.line_flow.count() == 1
    assert "本图无识别文本" in card.line_flow.itemAt(0).widget().text()


def test_line_flow_after_switch_image(qapp) -> None:
    """需求 7：切图后散行区随之重建。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("LINE-ONE\nLINE-TWO\nLINE-THREE")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    assert card.line_flow.count() == 3


def test_label_card_uses_light_theme(qapp) -> None:
    """需求 7：散行标签卡沿用应用**浅色主题**（浅底 + 深字，与 app.qss 一致）。

    v0.3.0 修正：v0.2.0 的卡片是「深色面 + 浅色字」，理由是"适配本机深色主题"，
    但 ``ui/styles/app.qss`` 实际是**浅色主题**（``#F5F6F8`` 底 / ``#303133`` 字），
    深色卡片落在白卡上形似渲染异常，故统一为浅色。
    """
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("RESIDUE-LINE")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    widget = card.line_flow.itemAt(0).widget()
    style = widget.styleSheet().replace(" ", "").lower()
    assert "#f5f7fa" in style  # 浅色面
    assert "#303133" in style  # 深色字
    assert "#2b2b2b" not in style  # 不再使用深色面


def test_flow_layout_activates_without_error(qapp) -> None:
    """需求 7：卡片真实显示时 FlowLayout 正常排版（不抛异常，高度按宽度自算）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("A\nB\nC")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    card.resize(420, 640)
    card.show()
    qapp.processEvents()
    assert card.line_flow.count() == 3
    assert card.line_flow.heightForWidth(300) > 0
    card.close()


# ── 需求 8：弹窗行为 ──


def test_view_ocr_button_opens_dialog(qapp) -> None:
    """需求 8：点击按钮 → 弹窗显示当前图 OCR 全文。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card._on_view_ocr()  # noqa: SLF001
    qapp.processEvents()
    assert card.ocr_dialog.isVisible() is True
    assert "AAA-BRAND-TEXT" in card.ocr_dialog.current_text()
    card.ocr_dialog.close()


def test_view_ocr_dialog_is_singleton(qapp) -> None:
    """需求 8：重复点击只置顶，不叠加第二个窗口。"""
    from PySide6.QtWidgets import QApplication

    card = _card(qapp)
    card.load_result(_three_image_result())
    dialog = card.ocr_dialog
    card._on_view_ocr()  # noqa: SLF001
    card._on_view_ocr()  # noqa: SLF001
    qapp.processEvents()
    found = [w for w in QApplication.topLevelWidgets() if w is dialog]
    assert found == [dialog]
    dialog.close()


def test_view_ocr_dialog_follows_current_image(qapp) -> None:
    """需求 8：切图后弹窗内容跟随刷新，且「上一张/下一张」同步可用性。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card._on_view_ocr()  # noqa: SLF001
    card.set_current_image("C:/img/2.jpg")
    qapp.processEvents()
    assert "BBB-MODEL-TEXT" in card.ocr_dialog.current_text()
    dialog = card.ocr_dialog
    # 第 1 张（下标 0）→ 无上一张，有下一张
    card.set_current_image("C:/img/1.jpg")
    qapp.processEvents()
    assert dialog.btn_prev.isEnabled() is False
    assert dialog.btn_next.isEnabled() is True
    # 最后一张 → 有上一张，无下一张
    card.set_current_image("C:/img/3.jpg")
    qapp.processEvents()
    assert dialog.btn_prev.isEnabled() is True
    assert dialog.btn_next.isEnabled() is False
    dialog.close()


def test_view_ocr_dialog_next_switches_image(qapp) -> None:
    """需求 8：「下一张」切换卡片当前图并同步左侧查看器（经证据图信号）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card._on_view_ocr()  # noqa: SLF001
    picked: list[str] = []
    card.evidence_selected.connect(picked.append)
    card.ocr_dialog.next_requested.emit()
    qapp.processEvents()
    assert card.current_image_path() == "C:/img/2.jpg"
    assert picked == ["C:/img/2.jpg"]
    card.ocr_dialog.close()


def test_view_ocr_dialog_does_not_cover_image_area(qapp) -> None:
    """需求 8 红线 R5：弹窗**不得覆盖图片区**（``overlap_with`` 硬断言）。"""
    from app.session import AppSession
    from ui.widgets.review_workbench import ReviewWorkbench

    session = AppSession()
    result = _three_image_result()
    session.replace([result], ticket_no="SA26090215")
    workbench = ReviewWorkbench(session)
    workbench.resize(1100, 620)
    workbench.show()
    qapp.processEvents()

    card = workbench.card
    card._on_view_ocr()  # noqa: SLF001
    qapp.processEvents()

    viewer = workbench.image_viewer
    dialog = card.ocr_dialog
    assert viewer.geometry().width() > 0
    assert dialog.isVisible() is True
    assert dialog.overlap_with(viewer) is False
    dialog.close()
    workbench.close()


def test_view_ocr_dialog_closed_with_card_clear(qapp) -> None:
    """需求 8：卡片 clear（切走记录）时弹窗一并隐藏。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card._on_view_ocr()  # noqa: SLF001
    qapp.processEvents()
    assert card.ocr_dialog.isVisible() is True
    card.clear()
    qapp.processEvents()
    assert card.ocr_dialog.isVisible() is False


# ══════════════════════════════════════════════════════════════════
#  需求 2：图片旋转（左旋 / 右旋 90°）
# ══════════════════════════════════════════════════════════════════


def test_rotation_starts_at_zero(qapp, tmp_path) -> None:
    """需求 2：加载后旋转角为 0°，标签显示 0°。"""
    viewer = _viewer(qapp, 400, 320)
    assert viewer.load(_make_png(tmp_path / "r0.png", 120, 60)) is True
    assert viewer.current_rotation() == 0
    assert "0" in viewer.rotation_label.text()


def test_rotate_right_adds_90(qapp, tmp_path) -> None:
    """需求 2：右旋一次 = +90°。"""
    viewer = _viewer(qapp, 400, 320)
    assert viewer.load(_make_png(tmp_path / "r1.png", 120, 60)) is True
    viewer.rotate_right()
    assert viewer.current_rotation() == 90
    viewer.rotate_left()
    assert viewer.current_rotation() == 0


def test_rotate_left_three_times_equals_right_once(qapp, tmp_path) -> None:
    """需求 2：左旋 3 次 ≡ 右旋 1 次。"""
    left = _viewer(qapp, 400, 320)
    right = _viewer(qapp, 400, 320)
    path = _make_png(tmp_path / "r2.png", 120, 60)
    left.load(path)
    right.load(path)
    for _ in range(3):
        left.rotate_left()
    right.rotate_right()
    assert left.current_rotation() == right.current_rotation() == 90


def test_rotate_left_four_times_returns_to_zero(qapp, tmp_path) -> None:
    """需求 2：同一方向旋转 4 次回到原位（0°）。"""
    viewer = _viewer(qapp, 400, 320)
    viewer.load(_make_png(tmp_path / "r3.png", 120, 60))
    for _ in range(4):
        viewer.rotate_left()
    assert viewer.current_rotation() == 0


def test_rotation_reset_on_load_and_clear(qapp, tmp_path) -> None:
    """需求 2：换图 / 清空自动摆正（旋转复位为 0°）。"""
    viewer = _viewer(qapp, 400, 320)
    viewer.load(_make_png(tmp_path / "r4.png", 120, 60))
    viewer.rotate_right()
    assert viewer.current_rotation() == 90
    viewer.load(_make_png(tmp_path / "r5.png", 120, 60))
    assert viewer.current_rotation() == 0
    viewer.rotate_left()
    viewer.clear()
    assert viewer.current_rotation() == 0


def test_rotated_display_size_swaps_axes(qapp, tmp_path) -> None:
    """需求 2：旋转 90° 后展示尺寸交换宽高（尺寸基准跟随旋转结果，防裁切）。"""
    viewer = _viewer(qapp, 400, 320)
    viewer.load(_make_png(tmp_path / "r6.png", 300, 100))
    before = viewer.display_pixmap().size()
    viewer.rotate_right()
    after = viewer.display_pixmap().size()
    assert (before.width(), before.height()) == (300, 100)
    assert (after.width(), after.height()) == (100, 300)


def test_rotation_auto_fits_to_window(qapp, tmp_path) -> None:
    """需求 2：旋转后自动 ``fit_to_window``（按**旋转后**尺寸重算缩放，避免出屏）。"""
    viewer = _viewer(qapp, 400, 320)
    viewer.load(_make_png(tmp_path / "r7.png", 900, 100))
    viewer.reset_zoom()  # 1:1
    qapp.processEvents()
    before = viewer.current_scale()
    viewer.rotate_right()  # 900×100 → 100×900，纵向远大于视口
    qapp.processEvents()
    assert viewer.current_rotation() == 90
    assert viewer.display_pixmap().size().height() == 900
    assert viewer.current_scale() < before  # 已按新尺寸重新适配


# ══════════════════════════════════════════════════════════════════
#  需求 3 + 4：记录表格 + 状态标签 + 模糊搜索 + 选中同步
# ══════════════════════════════════════════════════════════════════


def _mixed_session():
    """构造 4 条覆盖四种判定的会话（订单号 / 料号 / 票号各异，便于搜索断言）。"""
    from app.session import AppSession

    specs = [
        (Verdict.PASS, "2660326M", "N011901-007386-001"),
        (Verdict.FAIL, "2660310M", "N011901-009350-001"),
        (Verdict.NO_MARK, "2660311M", "N011901-008888-001"),
        (Verdict.NO_IMAGE, "2660312M", "N011901-007777-001"),
    ]
    results = []
    for verdict, order, part in specs:
        record = DeclarationRecord(
            ticket_no="SA26090215", part_no=part, order_no=order, decl_brand="X"
        )
        results.append(CheckResult(key=record.key(), record=record, verdict=verdict))
    session = AppSession()
    session.replace(results, ticket_no="SA26090215")
    return session


def _workbench(qapp):
    from ui.widgets.review_workbench import ReviewWorkbench

    return ReviewWorkbench(_mixed_session())


def test_record_table_has_three_columns(qapp) -> None:
    """需求 4：记录区为表格，三列「订单号 / 物料编号 / 核验结果」。"""
    workbench = _workbench(qapp)
    headers = [
        workbench.record_table.horizontalHeaderItem(col).text()
        for col in range(workbench.record_table.columnCount())
    ]
    assert headers == ["订单号", "物料编号", "核验结果"]
    assert workbench.record_table.rowCount() == 4
    assert not hasattr(workbench, "record_list")


def test_record_table_row_selection_is_single_and_highlighted(qapp) -> None:
    """需求 4：单行选择 + 选中行高亮（QSS 定义选中色）。"""
    from PySide6.QtWidgets import QAbstractItemView

    workbench = _workbench(qapp)
    table = workbench.record_table
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    style = table.styleSheet().replace(" ", "").lower()
    assert "item:selected" in style


def test_status_buttons_five_with_counts(qapp) -> None:
    """需求 3：5 个状态标签（全部/成功/待复核/缺图/失败），带数量与灯色。"""
    workbench = _workbench(qapp)
    keys = list(workbench.status_buttons)
    assert keys == [
        "all",
        Verdict.PASS.value,
        Verdict.NO_MARK.value,
        Verdict.NO_IMAGE.value,
        Verdict.FAIL.value,
    ]
    assert workbench.status_buttons["all"].text() == "全部 4"
    assert workbench.status_buttons[Verdict.PASS.value].text() == "成功 1"
    assert workbench.status_buttons[Verdict.NO_MARK.value].text() == "待复核 1"
    assert workbench.status_buttons[Verdict.NO_IMAGE.value].text() == "缺图 1"
    assert workbench.status_buttons[Verdict.FAIL.value].text() == "失败 1"


def test_status_button_filters_table(qapp) -> None:
    """需求 3：点「失败」标签 → 表格只剩校验异常一条。"""
    workbench = _workbench(qapp)
    workbench.status_buttons[Verdict.FAIL.value].click()
    assert workbench.current_status() == Verdict.FAIL.value
    assert workbench.record_table.rowCount() == 1
    assert workbench.filtered_keys() == [workbench.row_key(0)]


def test_status_button_counts_ignore_filter(qapp) -> None:
    """需求 3：标签数量取**全量**统计，不受当前过滤影响（防误导）。"""
    workbench = _workbench(qapp)
    workbench.status_buttons[Verdict.NO_MARK.value].click()
    assert workbench.status_buttons["all"].text() == "全部 4"
    assert workbench.status_buttons[Verdict.FAIL.value].text() == "失败 1"


def test_search_filters_by_order_no(qapp) -> None:
    """需求 3：模糊搜索命中订单号。"""
    workbench = _workbench(qapp)
    workbench.search_edit.setText("2660310")
    assert workbench.record_table.rowCount() == 1
    assert workbench.record_table.item(0, 0).text() == "2660310M"


def test_search_filters_by_part_no(qapp) -> None:
    """需求 3：模糊搜索命中物料编号（部分子串）。"""
    workbench = _workbench(qapp)
    workbench.search_edit.setText("9350")
    assert workbench.record_table.rowCount() == 1
    assert "009350" in workbench.record_table.item(0, 1).text()


def test_search_filters_by_ticket_no(qapp) -> None:
    """需求 3：模糊搜索命中出货单号（票号）。"""
    workbench = _workbench(qapp)
    workbench.search_edit.setText("sa26090215")
    assert workbench.record_table.rowCount() == 4


def test_search_is_case_insensitive(qapp) -> None:
    """需求 3：大小写不敏感。"""
    workbench = _workbench(qapp)
    workbench.search_edit.setText("n011901-007777")
    assert workbench.record_table.rowCount() == 1
    workbench.search_edit.setText("N011901-007777")
    assert workbench.record_table.rowCount() == 1


def test_search_combined_with_status(qapp) -> None:
    """需求 3：过滤管线 = 状态标签 → 模糊搜索（两条件叠加）。"""
    workbench = _workbench(qapp)
    workbench.status_buttons[Verdict.PASS.value].click()
    assert workbench.record_table.rowCount() == 1
    workbench.search_edit.setText("2660310")  # 属失败条 → 叠加后为空
    assert workbench.record_table.rowCount() == 0
    assert workbench.filtered_keys() == []


def test_empty_filter_result_shows_hint(qapp) -> None:
    """需求 3：过滤后无匹配 → 中间图片区显示提示，右侧卡片清空。"""
    workbench = _workbench(qapp)
    workbench.search_edit.setText("NO-SUCH-ORDER")
    assert workbench.record_table.rowCount() == 0
    assert "无匹配记录" in workbench.image_viewer.image_label.text()
    assert workbench.card.current_key() == ""


def test_row_selection_syncs_image_and_card(qapp, tmp_path) -> None:
    """需求 4：选中行 → 同步切中间图片与右侧结果。"""
    from app.session import AppSession
    from ui.widgets.review_workbench import ReviewWorkbench

    img_a = _make_png(tmp_path / "a.png", 80, 60)
    img_b = _make_png(tmp_path / "b.png", 120, 40)
    records = []
    for order, part, image in (("O-A", "P-A", img_a), ("O-B", "P-B", img_b)):
        record = DeclarationRecord(ticket_no="T", part_no=part, order_no=order)
        records.append(
            CheckResult(
                key=record.key(),
                record=record,
                verdict=Verdict.FAIL,
                evidence_images=[ImageEvidence(image_path=image, seq=1, exists=True)],
            )
        )
    session = AppSession()
    session.replace(records, ticket_no="T")
    workbench = ReviewWorkbench(session)
    workbench.resize(1100, 620)
    workbench.show()
    qapp.processEvents()

    workbench._select_row(1)  # noqa: SLF001
    qapp.processEvents()
    assert workbench.current_key() == records[1].key
    assert workbench.card.current_key() == records[1].key
    assert workbench.card.current_image_path() == img_b
    workbench.close()


def test_select_key_relaxes_filter(qapp) -> None:
    """需求 3：`select_key` 对当前过滤下不可见的记录自动放宽为「全部 + 清空搜索」。"""
    workbench = _workbench(qapp)
    workbench.status_buttons[Verdict.FAIL.value].click()
    target = workbench._all_results()[0].key  # noqa: SLF001 - 第 1 条是 PASS（当前不可见）
    assert workbench.select_key(target) is True
    assert workbench.current_status() == "all"
    assert workbench.search_edit.text() == ""


def test_review_pending_count(qapp) -> None:
    """头部「待复核：N 条」计数 = ⚠️ + 🔵。"""
    workbench = _workbench(qapp)
    assert workbench.review_pending_count() == 2
    assert "待复核：2 条" in workbench.pending_label.text()


# ══════════════════════════════════════════════════════════════════
#  P2-2：票号纠正入口
# ══════════════════════════════════════════════════════════════════


def _panel(qapp):
    from ui.widgets.data_source_panel import DataSourcePanel

    return DataSourcePanel()


def test_ticket_edit_button_exists(qapp) -> None:
    """P2-2：票号只读标签旁存在「改」按钮。"""
    panel = _panel(qapp)
    assert panel.ticket_edit_btn is not None
    assert panel.ticket_edit_btn.text() == "改"
    assert panel.ticket_edit.isReadOnly() is True


def test_edit_ticket_applies_new_value(qapp, monkeypatch) -> None:
    """P2-2：弹框确认后刷新票号标签，并同步到 inputs()。"""
    panel = _panel(qapp)
    panel.set_ticket_no("20260902")

    monkeypatch.setattr(
        "ui.widgets.data_source_panel.QInputDialog.getText",
        lambda *a, **k: ("SA26090301", True),
    )
    panel._on_edit_ticket()  # noqa: SLF001
    assert panel.ticket_edit.text() == "SA26090301"
    assert panel.inputs()["ticket_no"] == "SA26090301"


def test_edit_ticket_cancel_keeps_value(qapp, monkeypatch) -> None:
    """P2-2：取消弹框不改动票号。"""
    panel = _panel(qapp)
    panel.set_ticket_no("KEEP-ME")
    monkeypatch.setattr(
        "ui.widgets.data_source_panel.QInputDialog.getText",
        lambda *a, **k: ("IGNORED", False),
    )
    panel._on_edit_ticket()  # noqa: SLF001
    assert panel.ticket_edit.text() == "KEEP-ME"


def test_edit_ticket_blank_ignored(qapp, monkeypatch) -> None:
    """P2-2：留空不改动票号（避免误清空）。"""
    panel = _panel(qapp)
    panel.set_ticket_no("KEEP-ME")
    monkeypatch.setattr(
        "ui.widgets.data_source_panel.QInputDialog.getText",
        lambda *a, **k: ("   ", True),
    )
    panel._on_edit_ticket()  # noqa: SLF001
    assert panel.ticket_edit.text() == "KEEP-ME"


def test_edit_ticket_emits_inputs_changed(qapp, monkeypatch) -> None:
    """P2-2：纠正后发 inputs_changed（供主窗口回写配置）。"""
    panel = _panel(qapp)
    fired: list[int] = []
    panel.inputs_changed.connect(lambda: fired.append(1))
    monkeypatch.setattr(
        "ui.widgets.data_source_panel.QInputDialog.getText",
        lambda *a, **k: ("SA26090302", True),
    )
    panel._on_edit_ticket()  # noqa: SLF001
    assert fired == [1]


def test_edit_ticket_button_locked_while_running(qapp) -> None:
    """P2-2：运行期数据源锁定也覆盖「改」按钮。"""
    from app.run_controller import ControllerState

    panel = _panel(qapp)
    panel.set_state(ControllerState.RUNNING)
    assert panel.ticket_edit_btn.isEnabled() is False
    panel.set_state(ControllerState.IDLE_NO_BREAKPOINT)
    assert panel.ticket_edit_btn.isEnabled() is True


def test_preflight_can_override_manual_ticket(qapp) -> None:
    """P2-2：手改后「预检」仍可重新识别并覆盖（纠正可逆）。"""
    panel = _panel(qapp)
    panel.set_ticket_no("20260902")
    panel.set_ticket_no("SA26090215")  # 模拟预检识别覆盖
    assert panel.ticket_edit.text() == "SA26090215"
