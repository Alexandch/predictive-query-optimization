import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QHelpEvent, QTextCursor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

from pqo.sql_diff_view import (
    COLORS,
    MAX_CHARACTERS,
    LineChange,
    SQLDiffView,
    compare_sql_lines,
)


class SQLLineDiffTests(unittest.TestCase):
    def test_changed_lines_and_unchanged_context(self):
        left, right = compare_sql_lines(
            "SELECT id\nFROM t\nWHERE x=1\nORDER BY id",
            "SELECT id\nFROM t\nWHERE x=2\nORDER BY id",
        )
        self.assertEqual(set(left), {2})
        self.assertEqual(set(right), {2})
        self.assertEqual(right[2].previous, "WHERE x=1")
        self.assertEqual(right[2].kind, "changed")

    def test_added_lines_have_no_invented_old_sql(self):
        left, right = compare_sql_lines(
            "SELECT id\nFROM t", "SELECT id\nFROM t\nWHERE x=1"
        )
        self.assertFalse(left)
        self.assertEqual(set(right), {2})
        self.assertEqual(right[2].kind, "added")
        self.assertIn("не было", right[2].previous)

    def test_removed_lines_anchor_next_line_and_eof(self):
        left, right = compare_sql_lines(
            "SELECT * FROM (\nSELECT id FROM t\nORDER BY id\n) q",
            "SELECT * FROM (\nSELECT id FROM t\n) q",
        )
        self.assertEqual(left[2].kind, "removed")
        self.assertEqual(right[2].previous, "ORDER BY id")
        self.assertIn("перед", right[2].caption)
        left, right = compare_sql_lines(
            "SELECT id\nFROM t\nORDER BY id", "SELECT id\nFROM t"
        )
        self.assertEqual(right[1].previous, "ORDER BY id")
        self.assertIn("после", right[1].caption)

    def test_insertion_and_deletion_in_same_document(self):
        left, right = compare_sql_lines(
            "SELECT id\nFROM t\nWHERE x=1\nORDER BY id",
            "SELECT id\nFROM t\nORDER BY id\nLIMIT 10",
        )
        self.assertEqual(left[2].kind, "removed")
        self.assertIn("WHERE x=1", right[2].previous)
        self.assertEqual(right[3].kind, "added")

    def test_only_indent_is_ignored_but_literal_spaces_are_not(self):
        self.assertEqual(
            compare_sql_lines("SELECT id\n    FROM t", "SELECT id\n  FROM t"), ({}, {})
        )
        left, right = compare_sql_lines("SELECT 'one two'", "SELECT 'one  two'")
        self.assertTrue(left and right)
        for quote in ("'", "$$", '"'):
            original = "SELECT " + quote + "first\n  second" + quote
            variant = "SELECT " + quote + "first\n second" + quote
            self.assertTrue(compare_sql_lines(original, variant)[1], quote)

    def test_empty_identical_unfinished_and_unicode_sql(self):
        self.assertEqual(compare_sql_lines("SELECT 1", ""), ({}, {}))
        self.assertEqual(compare_sql_lines("", "SELECT 1"), ({}, {}))
        self.assertEqual(compare_sql_lines("SELECT 1", "SELECT 1"), ({}, {}))
        left, right = compare_sql_lines("SELECT 'unfinished", "SELECT 'незакончено")
        self.assertTrue(left and right)
        self.assertIn("unfinished", right[0].previous)

    def test_tooltip_is_literal_html_and_bounded(self):
        tooltip = LineChange("changed", "SELECT '<script>x</script>&value'").tooltip()
        self.assertNotIn("<script>", tooltip)
        self.assertIn("&lt;script&gt;", tooltip)
        self.assertIn("&amp;value", tooltip)
        self.assertLess(len(LineChange("changed", "x" * 100000).tooltip()), 6200)


class SQLDiffWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.source = QPlainTextEdit()
        self.variant = QPlainTextEdit()
        self.addCleanup(self.source.close)
        self.addCleanup(self.variant.close)
        self.view = SQLDiffView(self.source, self.variant)

    def set_sql(
        self, left="SELECT id\nFROM t\nWHERE x=1", right="SELECT id\nFROM t\nWHERE x=2"
    ):
        self.source.setPlainText(left)
        self.variant.setPlainText(right)
        self.view.refresh()

    def test_selections_do_not_modify_text_cursor_undo_or_verification_signals(self):
        self.set_sql()
        self.variant.moveCursor(QTextCursor.MoveOperation.End)
        self.variant.insertPlainText(" AND y=3")
        text = self.variant.toPlainText()
        cursor = self.variant.textCursor().position()
        modified = self.variant.document().isModified()
        changes = QSignalSpy(self.variant.textChanged)
        self.view.refresh()
        self.assertEqual(self.variant.toPlainText(), text)
        self.assertEqual(self.variant.textCursor().position(), cursor)
        self.assertEqual(self.variant.document().isModified(), modified)
        self.assertEqual(len(changes), 0)
        selections = self.variant.extraSelections()
        self.assertEqual(len(selections), 1)
        self.assertEqual(
            selections[0].format.background().color().name(), COLORS["changed"]
        )
        self.variant.undo()
        self.assertNotIn("AND y=3", self.variant.toPlainText())

    def test_hover_shows_old_line_not_raw_markup_and_nothing_on_unchanged_lines(self):
        self.set_sql()
        self.variant.resize(500, 250)
        self.variant.show()
        self.app.processEvents()
        cursor = QTextCursor(self.variant.document().findBlockByNumber(2))
        point = self.variant.cursorRect(cursor).center()
        event = QHelpEvent(
            QEvent.Type.ToolTip, point, self.variant.viewport().mapToGlobal(point)
        )
        with patch("pqo.sql_diff_view.QToolTip.showText") as show:
            QApplication.sendEvent(self.variant.viewport(), event)
        self.assertIn("WHERE x=1", show.call_args.args[1])
        point = self.variant.cursorRect(
            QTextCursor(self.variant.document().firstBlock())
        ).center()
        with patch("pqo.sql_diff_view.QToolTip.showText") as show:
            QApplication.sendEvent(
                self.variant.viewport(),
                QHelpEvent(
                    QEvent.Type.ToolTip,
                    point,
                    self.variant.viewport().mapToGlobal(point),
                ),
            )
            below = QPoint(30, 230)
            QApplication.sendEvent(
                self.variant.viewport(),
                QHelpEvent(
                    QEvent.Type.ToolTip,
                    below,
                    self.variant.viewport().mapToGlobal(below),
                ),
            )
        show.assert_not_called()

    def test_live_edit_recomputes_and_clear_discards_stale_marks(self):
        self.set_sql()
        self.variant.setPlainText("SELECT id\nFROM t\nWHERE x=3")
        self.assertFalse(self.view.variant_marks)
        QTest.qWait(120)
        self.assertEqual(self.view.variant_marks[2].previous, "WHERE x=1")
        self.variant.clear()
        self.view.refresh()
        self.assertFalse(self.source.extraSelections())
        self.assertFalse(self.variant.extraSelections())
        self.assertFalse(self.view.variant_marks)

    def test_oversized_comparison_does_not_start_quadratic_diff(self):
        self.source.setPlainText("x" * (MAX_CHARACTERS + 1))
        self.variant.setPlainText("SELECT 1")
        with patch("pqo.sql_diff_view.compare_sql_lines") as compare:
            self.view.refresh()
        compare.assert_not_called()
        self.assertTrue(self.view.limited)
        self.assertFalse(self.variant.extraSelections())

    def test_wrapped_changed_line_keeps_full_width_color_after_resize(self):
        self.variant.resize(180, 200)
        self.variant.show()
        self.set_sql("SELECT " + "old_column, " * 20, "SELECT " + "new_column, " * 20)
        self.app.processEvents()
        self.view.refresh()
        selections = self.variant.extraSelections()
        self.assertGreater(len(selections), 1)
        self.assertTrue(
            all(not selection.cursor.hasSelection() for selection in selections)
        )
        self.variant.resize(450, 200)
        QTest.qWait(120)
        self.assertLess(len(self.variant.extraSelections()), len(selections))
