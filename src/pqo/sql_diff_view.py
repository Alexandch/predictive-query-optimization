"""Read-only line annotations for two editable SQL documents; no SQL execution."""

from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from html import escape

from PyQt6.QtCore import QEvent, QObject, QRect, QTimer
from PyQt6.QtGui import QColor, QTextCursor, QTextFormat
from PyQt6.QtWidgets import QTextEdit, QToolTip


COLORS = {"changed": "#514219", "added": "#174532", "removed": "#582735"}
MAX_CHARACTERS = 500_000
MAX_LINES = 5_000


@dataclass(frozen=True, slots=True)
class LineChange:
    kind: str
    previous: str
    caption: str = "Исходный вариант"

    def tooltip(self):
        # Qt tooltips understand rich text: SQL must remain literal, not markup.
        previous = self.previous
        lines = previous.splitlines()
        if len(lines) > 40:
            previous = "\n".join(lines[:40]) + "\n…"
        if len(previous) > 6000:
            previous = previous[:6000] + "\n…"
        return (
            f"<b>{escape(self.caption)}</b>"
            f'<pre style="white-space: pre-wrap;">{escape(previous)}</pre>'
        )


def _line_keys(text, lines):
    """Ignore outer indentation, except inside multiline strings/identifiers."""
    from sqlglot import Dialect

    starts = [0]
    starts.extend(i + 1 for i, character in enumerate(text) if character == "\n")
    protected = set()
    try:
        for token in Dialect.get_or_raise("postgres").tokenize(text):
            first = bisect_right(starts, token.start) - 1
            last = bisect_right(starts, token.end) - 1
            if first != last:
                protected.update(range(first, last + 1))
    except Exception:
        # Unfinished manual SQL is still comparable, with no normalization.
        return lines
    return [line if i in protected else line.strip() for i, line in enumerate(lines)]


def compare_sql_lines(original, variant):
    """Return source/right annotations, including an anchor for deleted lines."""
    left, right = original.split("\n"), variant.split("\n")
    source_marks, variant_marks = {}, {}
    if not original.strip() or not variant.strip():
        return source_marks, variant_marks
    matcher = SequenceMatcher(
        None, _line_keys(original, left), _line_keys(variant, right)
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        previous = "\n".join(left[i1:i2])
        for line in range(i1, i2):
            source_marks[line] = LineChange(
                "removed" if tag == "delete" else "changed", previous
            )
        for line in range(j1, j2):
            variant_marks[line] = (
                LineChange(
                    "added", "В исходном SQL этой строки не было.", "Добавленная строка"
                )
                if tag == "insert"
                else LineChange("changed", previous)
            )
        if tag == "delete":
            anchor = min(j1, len(right) - 1)
            caption = (
                "Удалено перед этой строкой"
                if j1 < len(right)
                else "Удалено после этой строки"
            )
            existing = variant_marks.get(anchor)
            if existing:
                previous = existing.previous + "\n" + previous
                caption = "Исходный фрагмент, включая удалённые строки"
            variant_marks[anchor] = LineChange("changed", previous, caption)
    return source_marks, variant_marks


class SQLDiffView(QObject):
    """Highlight blocks without editing text, undo history, cursor or validation."""

    def __init__(self, source, variant):
        super().__init__(variant)
        self.source = source
        self.variant = variant
        self.source_marks = {}
        self.variant_marks = {}
        self.limited = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.refresh)
        for editor in (source, variant):
            editor.viewport().installEventFilter(self)
            editor.textChanged.connect(self._schedule)
        variant.setAccessibleDescription(
            "Изменённые строки выделены жёлтым, добавленные зелёным; "
            "удалённые строки слева красным. При наведении показан исходный вариант."
        )
        self.refresh()

    def _schedule(self):
        self._clear()
        self.timer.start()

    def _clear(self):
        self.source_marks = {}
        self.variant_marks = {}
        self.source.setExtraSelections([])
        self.variant.setExtraSelections([])
        QToolTip.hideText()

    def refresh(self):
        self.timer.stop()
        original, variant = self.source.toPlainText(), self.variant.toPlainText()
        self.limited = (
            max(len(original), len(variant)) > MAX_CHARACTERS
            or max(original.count("\n"), variant.count("\n")) >= MAX_LINES
        )
        self._clear()
        if self.limited:
            return
        self.source_marks, self.variant_marks = compare_sql_lines(original, variant)
        self._highlight(self.source, self.source_marks)
        self._highlight(self.variant, self.variant_marks)

    @staticmethod
    def _highlight(editor, marks):
        selections = []
        for line, mark in marks.items():
            block = editor.document().findBlockByNumber(line)
            if not block.isValid():
                continue
            layout = block.layout()
            # An empty selection gives a full-width background. Repeat for each
            # visual row when a physical SQL line wraps in the editor.
            for row in range(max(1, layout.lineCount())):
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(block)
                if layout.lineCount():
                    selection.cursor.setPosition(
                        block.position() + layout.lineAt(row).textStart()
                    )
                selection.format.setBackground(QColor(COLORS[mark.kind]))
                selection.format.setProperty(
                    QTextFormat.Property.FullWidthSelection, True
                )
                selections.append(selection)
        editor.setExtraSelections(selections)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.timer.start()
        if event.type() == QEvent.Type.ToolTip:
            if self.timer.isActive():
                self.refresh()
            editor = self.source if watched is self.source.viewport() else self.variant
            cursor = editor.cursorForPosition(event.pos())
            rect = editor.cursorRect(cursor)
            marks = self.source_marks if editor is self.source else self.variant_marks
            mark = (
                marks.get(cursor.blockNumber())
                if rect.top() <= event.pos().y() <= rect.bottom()
                else None
            )
            if mark:
                area = QRect(0, rect.top(), editor.viewport().width(), rect.height())
                QToolTip.showText(
                    event.globalPos(), mark.tooltip(), editor.viewport(), area
                )
            elif self.limited:
                QToolTip.showText(
                    event.globalPos(),
                    "Подсветка отключена для SQL больше 5000 строк или 500000 символов.",
                    editor.viewport(),
                )
            else:
                QToolTip.hideText()
            return True
        if event.type() in (
            QEvent.Type.Leave,
            QEvent.Type.Wheel,
            QEvent.Type.MouseButtonPress,
        ):
            QToolTip.hideText()
        return super().eventFilter(watched, event)
