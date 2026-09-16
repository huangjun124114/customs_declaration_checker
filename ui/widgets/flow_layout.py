"""流式布局（ui.widgets.flow_layout）—— Qt 官方示例的标准实现。

PySide6 **没有内置**流式布局；本模块按 Qt 官方 ``flowlayout`` 示例实现一个
可自动换行、按需换行重排的 :class:`QLayout`：

  * 子项优先**从左到右**排布，一行放不下则**换行**；
  * 高度随宽度自适应（``hasHeightForWidth() == True``），父容器可据
    :meth:`FlowLayout.heightForWidth` 精确分配高度；
  * 同时提供 ``count`` / ``itemAt`` / ``takeAt``，使其与 ``QLayout`` 的
    通用清空逻辑（``takeAt(0)`` 循环 + ``widget().deleteLater()``）兼容。

用途：复核工作台的「无键值对散行」标签卡（v0.2.0 点 9.4 副区）与「证据图号」
切换按钮区 —— 图/行数不固定时避免单行横向溢出。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget

__all__ = ["FlowLayout"]

#: 默认水平 / 垂直间距（当调用方显式传入负值时回退到此）
_DEFAULT_H_SPACING = 6
_DEFAULT_V_SPACING = 6


class FlowLayout(QLayout):
    """按行流式排布子项的布局（自动换行）。

    Args:
        parent: 父控件（可选；也可后续用 ``setLayout`` 装配）。
        margin: 四周内容边距（四个方向同值）。
        h_spacing: 水平间距；``< 0`` 时取默认值。
        v_spacing: 垂直间距；``< 0`` 时取默认值。

    Attributes:
        _items: 当前持有的布局子项列表（顺序即添加顺序）。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        h_spacing: int = -1,
        v_spacing: int = -1,
    ) -> None:
        """构造流式布局。"""
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = int(h_spacing)
        self._v_spacing = int(v_spacing)
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    # ─────────────────────── QLayout 接口 ───────────────────────

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt 覆写命名
        """追加一个布局子项。"""
        self._items.append(item)

    def count(self) -> int:
        """返回子项数量。"""
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt 覆写命名
        """返回指定位置的子项；越界返回 ``None``。"""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt 覆写命名
        """移除并返回指定位置的子项；越界返回 ``None``。"""
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt 覆写命名
        """不在任何方向"贪婪"扩展（由父容器按 content 高度分配）。"""
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 覆写命名
        """高度依赖宽度（流式换行必需）。"""
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 覆写命名
        """给定可用宽度，返回所需高度（用于父布局精确分配）。"""
        return self._do_layout(QRect(0, 0, int(width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt 覆写命名
        """按给定矩形重排全部子项。"""
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 覆写命名
        """推荐尺寸（取最小尺寸）。"""
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt 覆写命名
        """最小尺寸：所有子项最小尺寸的逐维最大值 + 边距。"""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    # ─────────────────────── 内部排布 ───────────────────────

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        """核心排布算法。

        Args:
            rect: 可用矩形（``test_only`` 时高度为 0，仅算高度）。
            test_only: ``True`` 只计算、不真正 ``setGeometry``。

        Returns:
            布局所需总高度（含下边距）。
        """
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0

        space_x = self._h_spacing if self._h_spacing >= 0 else _DEFAULT_H_SPACING
        space_y = self._v_spacing if self._v_spacing >= 0 else _DEFAULT_V_SPACING

        for item in self._items:
            size_hint = item.sizeHint()
            next_x = x + size_hint.width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + size_hint.width() + space_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), size_hint))
            x = next_x
            line_height = max(line_height, size_hint.height())

        return y + line_height - rect.y() + margins.bottom()
