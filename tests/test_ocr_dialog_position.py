"""``ui.widgets.ocr_text_dialog.OcrTextDialog.place`` 定位回归（v0.3.1 现场反馈修复）。

**现场反馈**：OCR 弹窗**置顶**且**压住图片区**。根因是旧实现按候选位依次试
「主窗口右侧外 → 图片区右侧外 → 图片区下方 → 屏幕左上角」，且要求候选位
**完整落在屏内**才采纳 —— 主窗口**最大化**时"右侧外"必然出屏，逐级退化后
最终落到**屏幕左上角 (8,8)**。

**新口径（用户裁定）**：

  1. 落在主窗口**右侧**；
  2. **垂直居中**（不再置顶）；
  3. 右侧空间不足时**按需收窄**（而不是溢出屏幕），仍不足则退到**图片区下方**，
     优先保住 R5「不遮图」。

因 :meth:`place` 是**纯函数**（只吃 ``QRect``），本文件可在**任意分辨率**下断言，
不受离屏平台固定屏幕尺寸（800×800）影响。

无 PySide6 环境整体 skip。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="无 PySide6，跳过 UI 用例")

from PySide6.QtCore import QRect  # noqa: E402

from ui.widgets.ocr_text_dialog import (  # noqa: E402
    _DEFAULT_HEIGHT,
    _DEFAULT_WIDTH,
    _GAP,
    _MIN_HEIGHT,
    _MIN_WIDTH,
    OcrTextDialog,
)

#: 各主流分辨率（宽 × 高）
_SCREENS = ((1920, 1080), (2560, 1440), (1600, 900), (1366, 768), (1280, 720))


def _right_gap(area: QRect, rect: QRect) -> int:
    """弹窗右边界到屏幕右边界的距离（``QRect`` 为闭区间语义，故可能差 1px）。"""
    return area.right() - rect.right()


# ══════════════════════════════════════════════════════════════════
#  ① 主口径：主窗口右侧 + 垂直居中（不置顶）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(("screen_w", "screen_h"), _SCREENS)
def test_right_aligned_and_vertically_centered(screen_w: int, screen_h: int) -> None:
    """主窗口最大化 + 图片区在左 → 弹窗**贴右侧**且**垂直居中**。"""
    area = QRect(0, 0, screen_w, screen_h)
    window = QRect(0, 0, screen_w, screen_h)
    viewer = QRect(0, 60, int(screen_w * 0.36), screen_h - 160)

    rect = OcrTextDialog.place(area, window, viewer)

    assert _GAP <= _right_gap(area, rect) <= _GAP + 1, "应贴主窗口右侧边界"
    assert abs(rect.center().y() - window.center().y()) <= 1, "应垂直居中"
    assert rect.intersects(viewer) is False, "不得遮挡图片区"
    assert area.contains(rect) is True, "应完整落在屏内"
    assert rect.top() > area.top() + _GAP, "不得置顶"


@pytest.mark.parametrize(("screen_w", "screen_h"), _SCREENS)
def test_not_pinned_to_top_but_centered(screen_w: int, screen_h: int) -> None:
    """**不置顶**的可证伪断言：弹窗上边界明显低于屏幕顶（≈ 屏高一半减半高）。"""
    area = QRect(0, 0, screen_w, screen_h)
    window = QRect(0, 0, screen_w, screen_h)
    viewer = QRect(0, 0, 600, screen_h)

    rect = OcrTextDialog.place(area, window, viewer)

    expected_top = window.center().y() - rect.height() // 2
    assert abs(rect.top() - expected_top) <= 1
    # 旧实现会落到 (8, 8) 附近 —— 该断言是对旧缺陷的回归锁
    assert rect.top() > 8


def test_floating_window_stays_inside_window_right_edge() -> None:
    """主窗口**非最大化**（浮动）时，右对齐目标 = 主窗口右边界（不越出窗口）。"""
    area = QRect(0, 0, 1920, 1080)
    window = QRect(100, 80, 1200, 700)
    viewer = QRect(110, 140, 430, 560)

    rect = OcrTextDialog.place(area, window, viewer)

    assert _GAP <= window.right() - rect.right() <= _GAP + 1
    assert abs(rect.center().y() - window.center().y()) <= 1
    assert rect.intersects(viewer) is False
    assert area.contains(rect) is True


def test_no_image_area_falls_back_to_right_half() -> None:
    """无图片区（``anchor=None``）→ 以**窗口中线**为左界，仍右对齐 + 垂直居中。"""
    area = QRect(0, 0, 1920, 1080)
    window = QRect(0, 0, 1920, 1080)

    rect = OcrTextDialog.place(area, window, None)

    assert _GAP <= _right_gap(area, rect) <= _GAP + 1
    assert abs(rect.center().y() - window.center().y()) <= 1
    assert rect.left() >= window.center().x(), "应落在右半区"


def test_no_window_at_all_is_screen_safe() -> None:
    """无主窗口（``win_geo=None``）→ 以屏幕为参照，不得抛异常且落在屏内。"""
    area = QRect(0, 0, 1920, 1080)

    rect = OcrTextDialog.place(area, None, None)

    assert area.contains(rect) is True


# ══════════════════════════════════════════════════════════════════
#  ② 按需收窄：右侧空间不足时收窄而不溢出
# ══════════════════════════════════════════════════════════════════


def test_narrow_right_area_shrinks_width() -> None:
    """图片区偏右使右侧剩余空间 < 默认宽 → 弹窗**收窄**至可用宽（下限 ``_MIN_WIDTH``）。"""
    area = QRect(0, 0, 1920, 1080)
    window = QRect(0, 0, 1920, 1080)
    # 右侧仅剩 480 - 2*GAP ≈ 464 可用
    viewer = QRect(0, 60, 1920 - 480, 900)

    rect = OcrTextDialog.place(area, window, viewer)

    assert rect.width() < _DEFAULT_WIDTH
    assert rect.width() >= _MIN_WIDTH
    assert rect.intersects(viewer) is False
    assert area.contains(rect) is True


def test_room_below_min_still_never_overflows_screen() -> None:
    """右侧连 ``_MIN_WIDTH`` 都放不下 → 退回兜底位，但**绝不溢出屏幕**。"""
    area = QRect(0, 0, 1024, 768)
    window = QRect(0, 0, 1600, 900)  # 窗口比屏幕宽（跨屏 / 缩放异常）
    viewer = QRect(0, 40, 700, 700)

    rect = OcrTextDialog.place(area, window, viewer)

    assert area.contains(rect) is True


# ══════════════════════════════════════════════════════════════════
#  ③ 兜底：右侧放不下 → 贴图片区下方（仍不遮图）
# ══════════════════════════════════════════════════════════════════


def test_small_screen_degrades_below_image_area() -> None:
    """小屏（图片区几乎占满）→ 退化为**图片区下方**，仍满足「不遮图」。"""
    area = QRect(0, 0, 800, 800)
    window = QRect(0, 0, 1216, 626)  # 离屏测试机的典型情形：窗口宽于屏幕
    viewer = QRect(404, 0, 320, 514)

    rect = OcrTextDialog.place(area, window, viewer)

    assert rect.intersects(viewer) is False
    assert rect.top() >= viewer.bottom() + _GAP, "应贴图片区下方"
    assert rect.height() >= _MIN_HEIGHT
    assert area.contains(rect) is True


def test_below_is_still_right_biased_and_not_top() -> None:
    """兜底位也**不置顶**：上边界低于图片区下边界。"""
    area = QRect(0, 0, 800, 800)
    window = QRect(0, 0, 1216, 626)
    viewer = QRect(404, 0, 320, 514)

    rect = OcrTextDialog.place(area, window, viewer)

    assert rect.top() > viewer.bottom()


# ══════════════════════════════════════════════════════════════════
#  ④ 屏幕收敛（clamp）与尺寸约束
# ══════════════════════════════════════════════════════════════════


def test_size_constants_are_coherent() -> None:
    """尺寸常量自洽：最小宽/高不超过默认宽/高。"""
    assert _MIN_WIDTH <= _DEFAULT_WIDTH
    assert _MIN_HEIGHT <= _DEFAULT_HEIGHT


def test_height_clamped_by_short_screen() -> None:
    """屏幕过矮 → 高度被压到 ``可用高 - 2*GAP``，不低于 ``_MIN_HEIGHT``。"""
    area = QRect(0, 0, 1920, 400)
    window = QRect(0, 0, 1920, 400)
    viewer = QRect(0, 0, 600, 400)

    rect = OcrTextDialog.place(area, window, viewer)

    assert rect.height() <= area.height() - 2 * _GAP
    assert rect.height() >= _MIN_HEIGHT
    assert area.contains(rect) is True
