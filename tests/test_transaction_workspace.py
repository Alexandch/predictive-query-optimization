"""Transaction geometry and focus changes must not alter SQL, checks or safety."""

import os
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication

from pqo.analysis_control import AnalysisControl
from pqo.desktop import MainWindow
from pqo.transaction_inspection import inspect_transaction


TEXT = """BEGIN;
SELECT enrollment_id, registered_at FROM university.enrollments
WHERE student_id = $1 ORDER BY registered_at DESC;
UPDATE university.students SET campus_id = $1 WHERE student_id = $2;
COMMIT;"""


class TransactionWorkspaceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # The offscreen platform has no Windows font fallback. Load the same
        # fonts as visual QA, otherwise Cyrillic glyph metrics are artificial.
        for name in ("arial.ttf", "arialbd.ttf", "consola.ttf"):
            QFontDatabase.addApplicationFont(
                str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name)
            )

    def setUp(self):
        previous_font = self.app.font()
        self.app.setFont(QFont("Arial", 10))
        self.addCleanup(lambda: self.app.setFont(previous_font))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.prefs = QSettings(
            str(Path(temporary.name) / "prefs.ini"), QSettings.Format.IniFormat
        )
        self.prefs.setValue("db_name", "source")
        self.prefs.setValue("allowed_schemas", "university")
        patcher = patch("pqo.desktop.QSettings", return_value=self.prefs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def window(self):
        window = MainWindow()
        self.addCleanup(window.close)
        window.resize(1180, 760)
        window.tabs.setCurrentIndex(1)
        window.show()
        self.app.processEvents()
        return window

    def mode(self, window, mode):
        window.transaction_workspace_mode.setCurrentIndex(
            window.transaction_workspace_mode.findData(mode)
        )
        self.app.processEvents()

    def inspect(self, window):
        window.transaction_editor.setPlainText(TEXT)
        window._transaction_inspected((TEXT, inspect_transaction(TEXT)))
        self.app.processEvents()

    def test_sql_editor_uses_workspace_before_analysis(self):
        window = self.window()
        self.assertEqual(window.transaction_workspace_mode.currentData(), "sql")
        self.assertTrue(window.transaction_editor.isVisible())
        self.assertFalse(window.transaction_analysis_panel.isVisible())
        self.assertGreater(window.transaction_editor.viewport().height(), 400)
        self.assertTrue(window.transaction_cancel_button.isVisible())
        self.assertLessEqual(window.width(), 1180)
        self.assertLessEqual(window.height(), 760)

    def test_analysis_navigation_preserves_tab_indices_and_expands_parameters(self):
        window = self.window()
        self.inspect(window)
        self.assertEqual(window.transaction_workspace_mode.currentData(), "analysis")
        self.assertFalse(window.transaction_source_panel.isVisible())
        self.assertFalse(window.transaction_protocol_panel.isVisible())
        self.assertFalse(window.transaction_views.tabBar().isVisible())
        self.assertEqual(window.transaction_sections.count(), 10)
        window.transaction_sections.setCurrentRow(5)
        self.app.processEvents()
        self.assertEqual(
            window.transaction_views.currentWidget(), window.transaction_parameter_page
        )
        self.assertEqual(window.transaction_parameter_table.rowCount(), 3)
        self.assertGreater(window.transaction_parameter_table.viewport().height(), 300)
        self.assertFalse(window.transaction_index_tools.isVisible())
        self.assertFalse(window.transaction_trial_tools.isVisible())
        for index in range(10):
            window.transaction_sections.setCurrentRow(index)
            self.assertEqual(window.transaction_views.currentIndex(), index)
        self.app.processEvents()
        self.assertTrue(
            window.transaction_sections.visualItemRect(
                window.transaction_sections.item(9)
            ).bottom()
            < window.transaction_sections.viewport().height()
        )

    def test_report_and_beside_reuse_actual_protocol_without_changing_sql(self):
        window = self.window()
        self.inspect(window)
        original = window.transaction_editor.toPlainText()
        inspection = window.transaction_inspection
        window.transaction_details.setPlainText("Evidence, not a recommendation")
        with patch("pqo.transaction_replay.psycopg.connect") as connect:
            window.transaction_protocol_check.setChecked(True)
            self.app.processEvents()
            self.assertTrue(window.transaction_views.isVisible())
            self.assertTrue(window.transaction_details.isVisible())
            window.transaction_protocol_button.click()
            self.app.processEvents()
            self.assertEqual(window.transaction_workspace_mode.currentData(), "report")
            self.assertFalse(window.transaction_views.isVisible())
            self.assertFalse(window.transaction_sections.isVisible())
            self.assertGreater(window.transaction_details.viewport().width(), 1000)
            self.assertGreater(window.transaction_details.viewport().height(), 400)
            window.transaction_protocol_button.click()
            self.assertEqual(
                window.transaction_workspace_mode.currentData(), "analysis"
            )
            self.mode(window, "split")
            self.mode(window, "sql")
        connect.assert_not_called()
        self.assertIs(window.transaction_inspection, inspection)
        self.assertEqual(
            window.transaction_details.toPlainText(), "Evidence, not a recommendation"
        )
        self.assertEqual(window.transaction_editor.toPlainText(), original)

    def test_resize_preferences_survive_focus_changes(self):
        window = self.window()
        self.mode(window, "split")
        window.transaction_workspace_splitter.setSizes([200, 300])
        window._save_transaction_panel_sizes()
        saved = window.transaction_workspace_splitter.sizes()
        window.transaction_protocol_check.setChecked(True)
        self.app.processEvents()
        window.transaction_result_splitter.setSizes([500, 300])
        window._save_transaction_panel_sizes()
        horizontal = window.transaction_result_splitter.sizes()
        self.mode(window, "sql")
        window._save_transaction_panel_sizes()
        self.mode(window, "report")
        window._save_transaction_panel_sizes()
        self.assertEqual(list(self.prefs.value("transaction_vertical_sizes")), saved)
        self.assertEqual(
            list(self.prefs.value("transaction_horizontal_sizes")), horizontal
        )
        self.mode(window, "split")
        restored = window.transaction_workspace_splitter.sizes()
        self.assertAlmostEqual(
            saved[0] / sum(saved), restored[0] / sum(restored), delta=0.03
        )
        for expected, actual in zip(
            horizontal, window.transaction_result_splitter.sizes()
        ):
            self.assertAlmostEqual(expected, actual, delta=3)

    def test_corrupt_preferences_fall_back_without_crash(self):
        self.prefs.setValue("transaction_vertical_sizes", ["bad", None])
        self.prefs.setValue("transaction_horizontal_sizes", [-1, 999999])
        self.prefs.setValue("transaction_workspace_mode", "unknown")
        window = self.window()
        self.assertEqual(window.transaction_workspace_mode.currentData(), "sql")
        self.assertEqual(
            window._transaction_saved_sizes("transaction_vertical_sizes", [220, 540]),
            [220, 540],
        )
        self.mode(window, "split")
        self.assertTrue(
            all(size > 60 for size in window.transaction_workspace_splitter.sizes())
        )

    def test_result_autofocus_does_not_overwrite_user_preference(self):
        window = self.window()
        self.mode(window, "sql")
        window.transaction_workspace_mode.activated.emit(
            window.transaction_workspace_mode.currentIndex()
        )
        self.inspect(window)
        self.assertEqual(window.transaction_workspace_mode.currentData(), "analysis")
        self.assertEqual(self.prefs.value("transaction_workspace_mode"), "sql")
        self.mode(window, "sql")
        window.transaction_editor.appendPlainText("-- local edit")
        self.assertEqual(window.transaction_workspace_mode.currentData(), "sql")
        self.assertIsNone(window.transaction_inspection)

    def test_expanded_rewrite_editors_keep_text_undo_and_validation(self):
        window = self.window()
        # Local-only generation uses the actual supported replacement fixture.
        from test_transaction_select_rewrite import TEXT as rewrite_text

        window.transaction_editor.setPlainText(rewrite_text)
        window._start_transaction_select_rewrites()
        deadline = time.monotonic() + 5
        while window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(window._active_workers)
        self.assertEqual(window.transaction_views.currentIndex(), 9)
        self.assertTrue(window._transaction_select_rewrite_options)
        editor = window.transaction_select_rewrite_variant
        before = editor.height()
        editor.insertPlainText("-- draft\n")
        text = editor.toPlainText()
        options = window._transaction_select_rewrite_options
        with patch(
            "pqo.transaction_select_rewrite_desktop.run_transaction_select_rewrite"
        ) as run:
            window.transaction_select_rewrite_expand.click()
            self.app.processEvents()
            self.assertFalse(window.transaction_select_rewrite_settings.isVisible())
            self.assertGreater(editor.height(), before + 100)
            self.assertGreater(editor.viewport().height(), 350)
            window.transaction_select_rewrite_expand.click()
            self.app.processEvents()
            self.assertTrue(window.transaction_select_rewrite_confirm.isVisible())
            self.assertFalse(window.transaction_select_rewrite_confirm.isChecked())
            self.assertFalse(window.transaction_select_rewrite_button.isEnabled())
        run.assert_not_called()
        self.assertIs(window._transaction_select_rewrite_options, options)
        self.assertEqual(editor.toPlainText(), text)
        self.assertTrue(editor.document().isUndoAvailable())
        editor.undo()
        self.assertNotEqual(editor.toPlainText(), text)
        self.assertEqual(window.transaction_editor.toPlainText(), rewrite_text)

    def test_focus_controls_never_unlock_execution_and_cancel_remains_visible(self):
        window = self.window()
        control = AnalysisControl()
        window._transaction_control = control
        window._set_query_actions_enabled(False)
        for mode in ("sql", "analysis", "split", "report"):
            self.mode(window, mode)
            window.transaction_select_rewrite_expand.setChecked(True)
            self.assertTrue(window.transaction_editor.isReadOnly())
            self.assertTrue(window.transaction_select_rewrite_variant.isReadOnly())
            self.assertFalse(window.transaction_inspect_button.isEnabled())
            self.assertFalse(window.transaction_select_rewrite_generate.isEnabled())
            self.assertFalse(window.transaction_replay_button.isEnabled())
            self.assertTrue(window.transaction_cancel_button.isVisible())
            self.assertTrue(window.transaction_cancel_button.isEnabled())
        window.transaction_cancel_button.click()
        self.assertTrue(control.cancelled.is_set())
        window._transaction_control = None
        window._set_query_actions_enabled(True)

    def test_replay_tables_use_alternate_tabs_not_stacked_small_panes(self):
        window = self.window()
        self.inspect(window)
        window.transaction_sections.setCurrentRow(8)
        self.app.processEvents()
        self.assertIs(
            window.transaction_replay_results.widget(0),
            window.transaction_replay_effect_table,
        )
        self.assertIs(
            window.transaction_replay_results.widget(1), window.transaction_replay_table
        )
        self.assertTrue(window.transaction_replay_confirm.isVisible())
        self.assertGreater(
            window.transaction_replay_effect_table.viewport().height(), 140
        )
        window.transaction_replay_results.setCurrentIndex(1)
        self.app.processEvents()
        self.assertGreater(window.transaction_replay_table.viewport().height(), 140)
