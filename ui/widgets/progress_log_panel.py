"""Zone 2：进度 + 日志面板（ui.widgets.progress_log_panel）。

**三区布局的第二区**（架构设计 12.B.2）：

  * **进度条**（``QProgressBar``）+ 「**N/M 条**」文本（``TaskProgress.progress_text()``）；
  * **阶段标签**（预检 / 探查 / 解析 / OCR / 导出 …）；
  * **日志视图**（``QPlainTextEdit``），按级别着色；
  * **级别过滤器**（``QComboBox``：全部 / DEBUG / INFO / WARN / ERROR）——**实时生效**
    （FR：日志级别过滤是活的，切换后立即重绘已累积日志）。

⚠️ **本文件是 UI 层**，可以 import PySide6；但不含任何判定逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.events import TaskPhase, TaskProgress
from infra.logger import LogLevel
from ui.styles.palette import Palette

__all__ = ["ProgressLogPanel", "LEVEL_ORDER"]

#: 级别严重度排序（用于「≥ 某级别」过滤）
LEVEL_ORDER: dict[str, int] = {
    LogLevel.DEBUG.value: 10,
    LogLevel.INFO.value: 20,
    LogLevel.WARN.value: 30,
    LogLevel.ERROR.value: 40,
}

_LEVEL_COLORS: dict[str, str] = {
    LogLevel.DEBUG.value: "#9E9E9E",
    LogLevel.INFO.value: "#D4D4D4",
    LogLevel.WARN.value: "#FFB74D",
    LogLevel.ERROR.value: "#EF5350",
}


class _LogEntry:
    """内存日志条目（保留全部，供级别过滤重绘）。"""

    __slots__ = ("text", "level")

    def __init__(self, text: str, level: str) -> None:
        self.text = text
        self.level = level


class ProgressLogPanel(QFrame):
    """进度 + 日志面板（第二区）。

    Args:
        parent: Qt 父控件。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """构造进度 + 日志面板。"""
        super().__init__(parent)
        self.setObjectName("zoneCard")
        self._entries: list[_LogEntry] = []
        self._filter_level: str = LogLevel.DEBUG.value  # 默认「全部」
        self._build_ui()

    # ─────────────────────── 构建 ───────────────────────

    def _build_ui(self) -> None:
        """搭建面板布局。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("② 进度 / 日志", self)
        title.setObjectName("zoneTitle")
        header.addWidget(title)
        header.addStretch(1)

        header.addWidget(QLabel("级别过滤：", self))
        self.level_combo = QComboBox(self)
        self.level_combo.addItem("全部（含 DEBUG）", LogLevel.DEBUG.value)
        self.level_combo.addItem("INFO 及以上", LogLevel.INFO.value)
        self.level_combo.addItem("WARN 及以上", LogLevel.WARN.value)
        self.level_combo.addItem("仅 ERROR", LogLevel.ERROR.value)
        self.level_combo.currentIndexChanged.connect(self._on_filter_changed)
        header.addWidget(self.level_combo)
        outer.addLayout(header)

        # 进度条行
        prog_row = QHBoxLayout()
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.count_label = QLabel("0/0 条", self)
        self.count_label.setMinimumWidth(90)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.phase_label = QLabel("阶段：待命", self)
        self.phase_label.setMinimumWidth(120)
        prog_row.addWidget(self.progress_bar, 1)
        prog_row.addWidget(self.count_label)
        prog_row.addWidget(self.phase_label)
        outer.addLayout(prog_row)

        # 日志视图
        self.log_view = QPlainTextEdit(self)
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        outer.addWidget(self.log_view, 1)

    # ─────────────────────── 公开 API ───────────────────────

    def apply_progress(self, progress: TaskProgress) -> None:
        """应用一条进度事件（更新进度条 / 计数 / 阶段 / 追加日志）。

        Args:
            progress: 进度事件。
        """
        pct = int(round(max(0.0, min(1.0, progress.fraction)) * 100))
        self.progress_bar.setValue(pct)
        self.count_label.setText(progress.progress_text())
        self.phase_label.setText(f"阶段：{progress.phase_label()}")

        if progress.message:
            self.append_log(progress.message, level=progress.level.value)

    def set_phase(self, phase: TaskPhase) -> None:
        """只更新阶段标签。"""
        from app.events import PHASE_LABELS

        self.phase_label.setText(f"阶段：{PHASE_LABELS.get(phase, phase.value)}")

    def append_log(self, text: str, *, level: str = LogLevel.INFO.value) -> None:
        """追加一条日志（保留全量，按当前过滤级别决定是否可见）。

        Args:
            text: 日志文本。
            level: 级别字符串（DEBUG/INFO/WARN/ERROR）。
        """
        entry = _LogEntry(text, level)
        self._entries.append(entry)
        if self._passes_filter(level):
            self._write_block(entry)

    def clear(self) -> None:
        """清空日志与进度（新一轮跑批前调用）。"""
        self._entries.clear()
        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.count_label.setText("0/0 条")
        self.phase_label.setText("阶段：待命")

    def reset_progress(self) -> None:
        """只重置进度条（不动日志）。"""
        self.progress_bar.setValue(0)
        self.count_label.setText("0/0 条")

    # ─────────────────────── 过滤 ───────────────────────

    def _passes_filter(self, level: str) -> bool:
        """当前过滤级别是否放行该条目。"""
        return LEVEL_ORDER.get(level, 20) >= LEVEL_ORDER.get(self._filter_level, 10)

    def _on_filter_changed(self, _index: int) -> None:
        """级别下拉变化 → 重绘全部日志（实时生效）。"""
        self._filter_level = str(self.level_combo.currentData() or LogLevel.DEBUG.value)
        self._repaint()

    def _repaint(self) -> None:
        """按当前过滤级别重绘日志视图。"""
        self.log_view.clear()
        for entry in self._entries:
            if self._passes_filter(entry.level):
                self._write_block(entry)

    def _write_block(self, entry: _LogEntry) -> None:
        """把一条日志以对应颜色写入视图（自动滚到底）。"""
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_LEVEL_COLORS.get(entry.level, Palette.TEXT)))
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(f"[{entry.level:<5}] {entry.text}\n", fmt)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    # ─────────────────────── 可见性（供测试）───────────────────────

    def visible_text(self) -> str:
        """返回当前日志视图的纯文本（供测试断言过滤效果）。"""
        return self.log_view.toPlainText()

    def visible_count(self) -> int:
        """返回当前可见日志行数。"""
        text = self.log_view.toPlainText()
        return len([ln for ln in text.splitlines() if ln.strip()])

    def entry_count(self) -> int:
        """返回已累积的日志条目总数。"""
        return len(self._entries)
