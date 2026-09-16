"""离屏 UI 预览：把复核工作台（含 OCR 弹窗）渲染成 PNG，供人工视觉验收。

**为什么需要它**：``QT_QPA_PLATFORM=offscreen`` 的虚拟屏固定为 **800×800**，
而现场是 1440p/1080p 宽屏 —— 直接 ``grab()`` 出来的图既不是现场分辨率，
弹窗定位也会退化到"贴图下方"，**看不出实际效果**。本工具因此：

  1. 用 **模拟屏幕尺寸**（默认 1920×1080）建一张画布；
  2. 把工作台按模拟窗口尺寸渲染进画布；
  3. 用 :meth:`ui.widgets.ocr_text_dialog.OcrTextDialog.place` 在**模拟坐标**上算出
     弹窗位置，再把弹窗渲染叠加到画布（等价于现场的双窗口叠加效果）。

用法::

    python tools/ui_preview.py                      # 默认 1920x1080 全屏窗口
    python tools/ui_preview.py --screen 1920x1080 --window 1600x900
    python tools/ui_preview.py --out 过程产出/ui_preview

**只读**：不写任何业务文件，只产出 PNG；输出目录默认落在调用方指定的位置。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import (  # noqa: E402
    CheckResult,
    DeclarationRecord,
    DifferenceDetail,
    ImageEvidence,
    OcrText,
    Verdict,
)
from core.token_matcher import MATCH_EXACT, MATCH_FUZZY, MATCH_NONE, TokenMatch  # noqa: E402
from ui.styles.palette import apply_stylesheet  # noqa: E402
from ui.widgets.ocr_text_dialog import _GAP, OcrTextDialog  # noqa: E402

#: 画布底色（与 app.qss 浅色主题一致，便于看清卡片边界）
_CANVAS_BG = "#EDEFF2"

#: 候选中文字体（离屏平台默认不带 CJK 字体 → 中文会渲染成豆腐块）
_CJK_FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Microsoft YaHei", "C:/Windows/Fonts/msyh.ttc"),
    ("Microsoft YaHei", "C:/Windows/Fonts/msyh.ttf"),
    ("SimHei", "C:/Windows/Fonts/simhei.ttf"),
    ("SimSun", "C:/Windows/Fonts/simsun.ttc"),
)


def _install_cjk_font(app: QApplication) -> str:
    """为离屏渲染装上中文字体（否则中文全是豆腐块）。

    **为什么必须显式装**：``QT_QPA_PLATFORM=offscreen`` 只用内置字体目录，
    不会回落到 Windows 系统字体；不装的话所有中文标签都渲染成 ``□``，
    截图完全无法做视觉验收。

    Returns:
        实际启用的字体名；找不到任何候选时返回空串（不抛异常）。
    """
    for family, path in _CJK_FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        if QFontDatabase.addApplicationFont(path) < 0:
            continue
        app.setFont(QFont(family, 9))
        return family
    return ""


def _parse_size(text: str, *, default: tuple[int, int]) -> tuple[int, int]:
    """解析 ``1920x1080`` 形式的尺寸串。"""
    try:
        w_str, _, h_str = str(text).lower().partition("x")
        return int(w_str), int(h_str)
    except (AttributeError, ValueError):
        return default


def _ocr(*lines: str, confidence: float = 0.92) -> OcrText:
    """构造一条 OCR 记录（``text_raw`` 为换行拼接的散行原文）。"""
    return OcrText(
        text_raw="\n".join(lines),
        confidence=confidence,
        line_scores=[confidence] * len(lines),
    )


def _build_session():
    """构造一个**贴近现场**的会话：含长判定原因 + 三段命中链路 + 多图。"""
    from app.session import AppSession

    brand_match = TokenMatch(
        field="品牌",
        declared="baori",
        mode=MATCH_EXACT,
        token="baori",
        images=[2],
        sample_line="AWM 20941 105C 90V VW-1 baori AWM 20941",
        note="",
    )
    model_match = TokenMatch(
        field="型号",
        declared="HS-8A50J-12",
        mode=MATCH_FUZZY,
        token="HS-8A50J-12",
        corrected_from="HS-8AS0J-12",
        images=[4, 6],
        sample_line="Handset HS-8A50-12 DAEWOO RoHS REACH",
        note="疑似误读，已纠正",
    )
    ok = CheckResult(
        key="SA2608015|N030107-003211-001|2660310M",
        verdict=Verdict.PASS,
        record=DeclarationRecord(
            ticket_no="SA2608015",
            part_no="N030107-003211-001",
            order_no="2660310M",
            decl_brand="baori",
            decl_model="HS-8A50J-12",
        ),
        detected_brand="baori",
        detected_model="HS-8A50J-12",
        reason="申报值与图片识别值一致，判合格",
        evidence_images=[
            ImageEvidence(
                image_path="C:/img/1.jpg",
                exists=True,
                seq=1,
                ocr=_ocr(
                    "LG LCD / LED / OLED Television Limited Warranty - USA",
                    "ARBITRATION NOTICE: THIS LIMITED WARRANTY CONTAINS AN",
                    "Printed in China",
                    "WARRANTYPERIOD",
                ),
            ),
            ImageEvidence(
                image_path="C:/img/2.jpg",
                exists=True,
                seq=2,
                ocr=_ocr("AWM 20941 105C 90V VW-1", "baori", "E339609"),
            ),
            ImageEvidence(
                image_path="C:/img/4.jpg",
                exists=True,
                seq=4,
                ocr=_ocr("Handset HS-8A50-12 DAEWOO RoHS REACH"),
            ),
            ImageEvidence(
                image_path="C:/img/6.jpg",
                exists=True,
                seq=6,
                ocr=_ocr("MODEL:HS-8AA", "HS-8A50J-12 K1 007226"),
            ),
        ],
    )
    ok.token_matches = [brand_match, model_match]

    # ⚠️ 刻意构造**长判定原因**（多差异点 + 嵌套备注）以压测表格下方文案区，
    #    复现现场截图里"文案挤在一起"的排版压力。
    bad = CheckResult(
        key="SA2608015|N080102-000528-002|2660229F",
        verdict=Verdict.FAIL,
        record=DeclarationRecord(
            ticket_no="SA2608015",
            part_no="N080102-000528-002",
            order_no="2660229F",
            decl_brand="",
            decl_model="",
        ),
        detected_brand="SKYHORTH",
        detected_model="",
        reason=(
            "品牌：申报『』与图片『SKYWORTH P/N』明确不一致"
            "（已知 OCR 误读『SKYHORTH』已纠正为『SKYWORTH P/N』）"
        ),
        differences=[
            DifferenceDetail(
                field="品牌",
                declared_value="",
                detected_value="SKYHORTH",
                char_diffs=["图片多出『SKYHORTH』（第0位起）"],
                note="外箱整机品牌，不构成本体证据",
            )
        ],
        evidence_images=[
            ImageEvidence(
                image_path="C:/img/b1.jpg",
                exists=True,
                seq=1,
                ocr=_ocr(
                    "LG",
                    "*13*",
                    "Printed in China",
                    "LG LCD / LED / OLED Television Limited Warranty - USA",
                    "ARBITRATION NOTICE: THIS LIMITED WARRANTY CONTAINS AN",
                    "BINDING ARBITRATION INSTEAD OF IN COURT, UNLESS YOU CHOOSE",
                    "WARRANTYPERIOD",
                    "WHAT IS COVERED",
                    "Parts and Labor",
                    "· Replacement products and parts are warranted for the",
                ),
            ),
            ImageEvidence(image_path="C:/img/b2.jpg", exists=True, seq=2, ocr=_ocr("SKYHORTH P/N")),
        ],
    )
    bad.token_matches = [
        TokenMatch(
            field="品牌",
            declared="",
            mode=MATCH_NONE,
            token="",
            images=[],
            sample_line="SKYHORTH P/N",
            note="命中作废：图内该词为字段名",
        ),
        TokenMatch(
            field="型号",
            declared="",
            mode=MATCH_NONE,
            token="",
            images=[],
            sample_line="",
            note="申报为无",
        ),
    ]

    session = AppSession()
    session.replace([ok, bad], ticket_no="SA2608015")
    return session, bad.key


def render(
    *,
    screen: tuple[int, int] = (1920, 1080),
    window: tuple[int, int] | None = None,
    out_dir: Path,
    open_dialog: bool = True,
) -> list[Path]:
    """渲染预览图并返回产出路径列表。

    Args:
        screen: 模拟屏幕尺寸（宽, 高）。
        window: 模拟主窗口尺寸；``None`` 表示与屏幕同尺寸（最大化）。
        out_dir: PNG 输出目录（不存在则创建）。
        open_dialog: 是否叠加 OCR 弹窗。

    Returns:
        产出的 PNG 路径列表。
    """
    app = QApplication.instance() or QApplication([])
    # ① 装中文字体（离屏平台默认无 CJK）② 套全局 QSS（与现场一致）
    _install_cjk_font(app)
    apply_stylesheet(app)
    win_w, win_h = window or screen

    session, bad_key = _build_session()

    from ui.widgets.review_workbench import ReviewWorkbench

    workbench = ReviewWorkbench(session)
    workbench.resize(win_w, win_h)
    workbench.show()
    app.processEvents()
    workbench.select_key(bad_key)
    app.processEvents()

    area = QRect(0, 0, screen[0], screen[1])
    win_geo = QRect(0, 0, win_w, win_h)

    canvas = QPixmap(screen[0], screen[1])
    canvas.fill(QColor(_CANVAS_BG))

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    # 主窗口**只画窗口区域**（工作台可能因最小宽 > 模拟窗口而横向溢出）
    painter.setClipRect(QRect(0, 0, win_w, win_h))
    workbench.render(painter, QRect(0, 0, win_w, win_h).topLeft())
    painter.setClipping(False)

    outputs: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    if open_dialog:
        dialog = workbench.card.ocr_dialog
        workbench.card._on_view_ocr()  # noqa: SLF001 - 预览工具，直接走现场同一入口
        app.processEvents()
        viewer_geo = workbench.image_viewer.geometry()
        rect = OcrTextDialog.place(area, win_geo, viewer_geo)
        dialog.setGeometry(rect)
        app.processEvents()

        # 几何自检：右侧 / 垂直居中 / 不遮图 —— 与 test_ocr_dialog_position 同口径
        checks = {
            "右对齐到窗口右边界(±1)": abs((win_geo.right() - _GAP) - rect.right()) <= 1,
            "垂直居中(±1)": abs(rect.center().y() - win_geo.center().y()) <= 1,
            "不遮图片区": not rect.intersects(viewer_geo),
            "完整落在屏内": area.contains(rect),
        }
        print(f"图片区矩形：{viewer_geo.left()},{viewer_geo.top()} "
              f"{viewer_geo.width()}x{viewer_geo.height()}")
        print(f"弹窗矩形　：{rect.left()},{rect.top()} {rect.width()}x{rect.height()}")
        for name, ok in checks.items():
            print(f"  [{'OK ' if ok else 'FAIL'}] {name}")

        painter.setClipRect(rect)
        dialog.render(painter, rect.topLeft())
        painter.setClipping(False)
        # 弹窗边框（区分两个窗口）
        painter.setPen(QColor("#1565C0"))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()

        path = out_dir / "preview_workbench_with_dialog.png"
        canvas.save(str(path), "PNG")
        outputs.append(path)
        dialog.close()
    else:
        painter.end()
        path = out_dir / "preview_workbench.png"
        canvas.save(str(path), "PNG")
        outputs.append(path)

    workbench.close()
    return outputs


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="复核工作台离屏 UI 预览")
    parser.add_argument("--screen", default="1920x1080", help="模拟屏幕尺寸，如 1920x1080")
    parser.add_argument("--window", default="", help="模拟主窗口尺寸；缺省 = 屏幕尺寸（最大化）")
    parser.add_argument(
        "--out",
        default=str(_ROOT.parent / "过程产出" / "ui_preview"),
        help="PNG 输出目录",
    )
    parser.add_argument("--no-dialog", action="store_true", help="不叠加 OCR 弹窗")
    args = parser.parse_args(argv)

    screen = _parse_size(args.screen, default=(1920, 1080))
    window = _parse_size(args.window, default=screen) if args.window else None
    out_dir = Path(args.out)

    paths = render(
        screen=screen,
        window=window,
        out_dir=out_dir,
        open_dialog=not args.no_dialog,
    )
    print(f"模拟屏幕：{screen[0]}x{screen[1]}")
    print(f"模拟窗口：{(window or screen)[0]}x{(window or screen)[1]}")
    for path in paths:
        print(f"[OK] 预览图：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
