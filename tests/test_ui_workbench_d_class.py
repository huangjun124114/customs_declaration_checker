"""批次 3-B：人工复核工作台 D 类（点 9.1–9.4）+ 票号纠正入口（P2-2）。

在 ``QT_QPA_PLATFORM=offscreen`` 下用**夹具构造的 CheckResult**（不跑真实跑批）
验证纯 UI 行为：

  * **9.1 拖动平移**：``setWidgetResizable(False)``；放大超出视口后
    ``horizontalScrollBar().maximum() > 0``（原缺陷下为 0）；左键拖拽改变滚动值；
    抓手光标**仅在溢出时**出现；``Ctrl + 滚轮`` 缩放保留。
  * **9.2 只显当前图**：带 3 张图的记录，切换后 ``evidence_view`` **只含当前图**文本
    （可证伪回归锁：原缺陷下会拼接全部 3 张）。
  * **9.3 四段式判定链路**：① 申报要素 / ② 图片识别（+ 证据来源）/ ③ 判定原因
    （逐字符差异）/ ④ 判定结果 —— 四段均有内容。
  * **9.4 KV 展示**：有 KV → 表格出行（键/值/置信度）；无 KV → 进散行标签卡；
    原文可折叠。
  * **P2-2 票号纠正入口**：只读标签旁的「改」按钮弹 ``QInputDialog``，改完刷新；
    取消 / 留空不改；运行期置灰。

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


def test_evidence_view_only_current_image(qapp) -> None:
    """9.2 可证伪回归锁：默认只显第 1 张图 OCR，不含另两张文本。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.evidence_view.toPlainText()
    assert "AAA-BRAND-TEXT" in text
    assert "BBB-MODEL-TEXT" not in text
    assert "CCC-RESIDUE-TEXT" not in text


def test_switch_image_updates_evidence_view(qapp) -> None:
    """9.2：切到第 2 张图后，evidence_view 只含第 2 张文本（不含第 1、3 张）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card.set_current_image("C:/img/2.jpg")
    text = card.evidence_view.toPlainText()
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
    result.verdict = Verdict.NO_MARK  # 工作台默认只列待复核（⚠️/🔵）记录
    session.replace([result], ticket_no="SA26090215")
    workbench = ReviewWorkbench(session)
    workbench._on_evidence_selected("C:/img/2.jpg")  # noqa: SLF001
    assert "BBB-MODEL-TEXT" in workbench.card.evidence_view.toPlainText()


# ══════════════════════════════════════════════════════════════════
#  9.3 四段式判定链路
# ══════════════════════════════════════════════════════════════════


def test_chain_declared_section_has_content(qapp) -> None:
    """9.3 ① 申报要素：品牌 / 型号来自 record.decl_*。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_labels["decl"].text()
    assert "申报要素" in text
    assert "baori" in text
    assert "A7A01G" in text


def test_chain_detected_section_has_content(qapp) -> None:
    """9.3 ② 图片识别：来自 detected_*（跨图投票结果）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_labels["detected"].text()
    assert "图片识别" in text
    assert "Daewoo" in text
    assert "HS-8AA" in text


def test_chain_reason_section_lists_char_diffs(qapp) -> None:
    """9.3 ③ 判定原因：含差异字段与逐字符差异点。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_labels["reason"].text()
    assert "判定原因" in text
    assert "品牌" in text
    assert "b→D" in text


def test_chain_verdict_section_has_content(qapp) -> None:
    """9.3 ④ 判定结果：含四类判定字符串（口径逐字）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    text = card.chain_labels["verdict"].text()
    assert "判定结果" in text
    assert VERDICT_FAIL in text


def test_chain_reason_falls_back_to_reason_text(qapp) -> None:
    """9.3 ③：无差异明细时回退到 result.reason。"""
    card = _card(qapp)
    result = _three_image_result()
    result.differences = []
    result.reason = "申报缺失但图片明确有"
    card.load_result(result)
    assert "申报缺失但图片明确有" in card.chain_labels["reason"].text()


def test_chain_brand_source_hint_lists_images(qapp) -> None:
    """9.3 ②：品牌证据来源（best-effort）能列出含该品牌的图号。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[0].ocr = _ocr("Daewoo AAA")
    result.evidence_images[1].ocr = _ocr("Daewoo BBB")
    result.evidence_images[2].ocr = _ocr("CCC")
    card.load_result(result)
    text = card.chain_labels["detected"].text()
    assert "图1" in text
    assert "2 张图" in text


def test_chain_brand_source_hint_marks_missing(qapp) -> None:
    """9.3 ②：推断不出证据来源时显式标注「未记录」（绝不臆造）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.detected_brand = "NoSuchBrand"
    card.load_result(result)
    assert "证据来源未记录" in card.chain_labels["detected"].text()


# ══════════════════════════════════════════════════════════════════
#  9.4 KV 表格 + 散行标签卡 + 折叠原文
# ══════════════════════════════════════════════════════════════════


def test_kv_table_rows_when_kv_present(qapp) -> None:
    """9.4 主区：有键值对 → 表格按行展示（键 / 值 / 置信度）。"""
    card = _card(qapp)
    card.load_result(_three_image_result())  # 当前图 = 图1，kv={"Brand": "Daewoo"}
    assert card.kv_table.rowCount() == 1
    assert card.kv_table.item(0, 0).text() == "Brand"
    assert card.kv_table.item(0, 1).text() == "Daewoo"


def test_kv_table_confidence_from_line_scores(qapp) -> None:
    """9.4 主区：置信度列取对应行分数（92%）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[0].ocr = _ocr(
        "Brand:Daewoo", kv={"Brand": "Daewoo"}, line_scores=[0.92]
    )
    card.load_result(result)
    assert card.kv_table.item(0, 2).text() == "92%"


def test_kv_table_empty_when_no_kv(qapp) -> None:
    """9.4：当前图无键值对 → KV 表无行。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card.set_current_image("C:/img/3.jpg")  # 该图 kv 为空
    assert card.kv_table.rowCount() == 0


def test_residue_label_cards_when_no_kv(qapp) -> None:
    """9.4 副区：无键值对时，每条散行进一个标签卡。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("LINE-ONE\nLINE-TWO\nLINE-THREE")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    assert card.residue_flow.count() == 3


def test_residue_excludes_kv_lines(qapp) -> None:
    """9.4 副区：已成 KV 的行不进散行标签卡。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[0].ocr = _ocr(
        "Brand:Daewoo\nModel:HS-8AA\nMADE IN CHINA",
        kv={"Brand": "Daewoo", "Model": "HS-8AA"},
    )
    card.load_result(result)
    assert card.residue_flow.count() == 1
    only = card.residue_flow.itemAt(0).widget()
    assert only.text() == "MADE IN CHINA"


def test_raw_text_toggle_collapsed_by_default(qapp) -> None:
    """9.4 折叠区：原文默认隐藏。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    assert card.raw_view.isHidden() is True
    assert card.raw_toggle.isChecked() is False


def test_raw_text_toggle_shows_original(qapp) -> None:
    """9.4 折叠区：展开后显示当前图原始 OCR 全文。"""
    card = _card(qapp)
    card.load_result(_three_image_result())
    card.raw_toggle.setChecked(True)
    assert card.raw_view.isHidden() is False
    assert card.raw_view.toPlainText() == "AAA-BRAND-TEXT"


def test_label_card_uses_dark_surface(qapp) -> None:
    """9.4：散行标签卡为深色面 + 浅色字（适配深色主题，非白底黑字）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("RESIDUE-LINE")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    widget = card.residue_flow.itemAt(0).widget()
    style = widget.styleSheet().replace(" ", "").lower()
    assert "#2b2b2b" in style  # 深色面
    assert "#e0e0e0" in style  # 浅色字


def test_flow_layout_activates_without_error(qapp) -> None:
    """9.4：卡片真实显示时 FlowLayout 正常排版（不抛异常，高度按宽度自算）。"""
    card = _card(qapp)
    result = _three_image_result()
    result.evidence_images[2].ocr = _ocr("A\nB\nC")
    card.load_result(result)
    card.set_current_image("C:/img/3.jpg")
    card.resize(420, 640)
    card.show()
    qapp.processEvents()
    assert card.residue_flow.count() == 3
    assert card.residue_flow.heightForWidth(300) > 0
    card.close()


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
