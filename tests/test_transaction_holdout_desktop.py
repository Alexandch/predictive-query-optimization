import os
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_comparison import TransactionIndexComparison, source_context_digest, sql_digest
from pqo.transaction_holdout import TransactionHoldoutReport, prepare_holdout
from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
from pqo.transaction_indexes import RelationIndexes, build_transaction_index_report
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport

ROOT = Path(__file__).resolve().parents[1]


class TransactionHoldoutDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preferences = QSettings(str(Path(directory.name) / "prefs.ini"), QSettings.Format.IniFormat)
        preferences.setValue("allowed_schemas", "university")
        preferences.setValue("db_name", "source")
        patcher = patch("pqo.desktop.QSettings", return_value=preferences)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, window, *, stable=True):
        source = (ROOT / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        control_sql = (ROOT / "examples/transactions/university-index-holdout.sql").read_text(encoding="utf-8")
        window.transaction_editor.setPlainText(source)
        settings = window._database_settings()
        inspection = inspect_transaction(source)
        plans = TransactionPlanReport(inspection, settings.dbname, tuple(StatementPlan(item.number, "planned", "fixture") for item in inspection.statements))
        window._transaction_inspected((source, inspection))
        window._transaction_planned((source, settings, plans))
        relation = RelationIndexes("university", "enrollments", ("enrollment_id", "student_id", "offering_id", "registered_at", "status"))
        indices = build_transaction_index_report(inspection, plans, {("university", "enrollments"): relation})
        window._transaction_indexes_ready((source, settings, indices))
        for key in ("student_id", "offering_id"):
            row = next(i for i, candidate in enumerate(indices.candidates) if candidate.action.key_columns == (key, "registered_at") and not candidate.action.include_columns)
            window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        window.transaction_copy_edit.setText("lab")
        window.transaction_copy_check.setChecked(True)
        actions = window._checked_transaction_indexes()
        trial = TransactionIndexTrial(actions[0], "lab", "measured",
            (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True), TrialReading(2, (100.,)*3, (10.,)*3, (100.,)*3, True)),
            (3,), (), True, True, additional_actions=actions[1:], frequencies=((1, 1), (2, 1)))
        comparison = TransactionIndexComparison("lab", ((1, 1), (2, 1)), (trial, trial), 2, True, 0 if stable else None, (),
            requested_series=2, completed_series=2, coordinates=((1, 1), (2, 1)), series_preferences=(1, 1),
            development_sql_digest=sql_digest(source), source_context_digest=source_context_digest(settings))
        window._transaction_comparison_ready((source, settings, "lab", actions, comparison.frequencies, source, 2, comparison))
        window.transaction_views.setCurrentIndex(6)
        return source, control_sql, settings, comparison, trial

    def result(self, window, control_sql, trial):
        selection = window._transaction_holdout_selection
        control_trial = replace(trial, skipped_writes=(), readings=trial.readings + (replace(trial.readings[0], number=3),),
            frequencies=((1, 1), (2, 1), (3, 1)))
        return TransactionHoldoutReport(selection, prepare_holdout(control_sql, selection, window._database_settings()),
            (control_trial, control_trial), True, True, ())

    def test_freeze_before_control_input_and_cannot_refreeze_another_row(self):
        window = MainWindow()
        try:
            source, control_sql, settings, comparison, trial = self.fixture(window)
            self.assertTrue(window.transaction_holdout_editor.isReadOnly())
            self.assertFalse(window.transaction_holdout_load_button.isEnabled())
            self.assertTrue(window.transaction_holdout_freeze_button.isEnabled())
            window._freeze_transaction_holdout()
            selection = window._transaction_holdout_selection
            self.assertFalse(window.transaction_holdout_editor.isReadOnly())
            self.assertFalse(window.transaction_holdout_freeze_button.isEnabled())
            window.transaction_compare_table.selectRow(1)
            window._freeze_transaction_holdout()
            self.assertIs(window._transaction_holdout_selection, selection)
            self.assertEqual(window.transaction_editor.toPlainText(), source)
            self.assertIs(window.transaction_compare_report, comparison)
        finally:
            window.close()

    def test_invalid_input_or_declined_confirmation_does_not_consume_attempt(self):
        window = MainWindow()
        try:
            source, control_sql, settings, comparison, trial = self.fixture(window)
            window._freeze_transaction_holdout()
            window.transaction_holdout_editor.setPlainText("UPDATE university.enrollments SET status='x';")
            with patch.object(window, "_start_worker") as start:
                window._start_transaction_holdout()
            start.assert_not_called()
            self.assertFalse(window._transaction_holdout_attempted)
            window.transaction_holdout_editor.setPlainText(control_sql)
            with patch("pqo.transaction_holdout_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.No), patch.object(window, "_start_worker") as start:
                window._start_transaction_holdout()
            start.assert_not_called()
            self.assertFalse(window._transaction_holdout_attempted)
        finally:
            window.close()

    def test_real_queued_worker_locks_all_actions_preserves_development_and_spends_one_attempt(self):
        window = MainWindow()
        try:
            source, control_sql, settings, comparison, trial = self.fixture(window)
            window._freeze_transaction_holdout()
            window.transaction_holdout_editor.setPlainText(control_sql)
            result = self.result(window, control_sql, trial)
            with patch("pqo.transaction_holdout_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                    patch("pqo.transaction_holdout_desktop.validate_index_holdout", return_value=result) as run:
                window._start_transaction_holdout()
                self.assertFalse(window.analyze_button.isEnabled())
                self.assertFalse(window.transaction_compare_button.isEnabled())
                self.assertTrue(window.transaction_holdout_editor.isReadOnly())
                self.assertTrue(window.transaction_cancel_button.isEnabled())
                deadline = time.monotonic() + 5
                while window._active_workers and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.005)
            self.assertFalse(window._active_workers)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertIs(window.transaction_holdout_report, result)
            self.assertEqual(window.transaction_holdout_table.rowCount(), 2)
            self.assertIn("Подбор + контроль: пройдены", window.transaction_details.toPlainText())
            self.assertTrue(window.transaction_holdout_editor.isReadOnly())
            self.assertFalse(window.transaction_holdout_run_button.isEnabled())
            self.assertIs(window.transaction_compare_report, comparison)
            self.assertEqual(window.transaction_editor.toPlainText(), source)
            with patch.object(window, "_start_worker") as start:
                window._start_transaction_holdout()
            start.assert_not_called()
            self.assertEqual(run.call_args.args[0], result.selection)
            self.assertEqual(run.call_args.args[1], control_sql)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_control_success_does_not_promote_manual_hypothesis(self):
        window = MainWindow()
        try:
            source, control_sql, settings, comparison, trial = self.fixture(window, stable=False)
            window._freeze_transaction_holdout()
            self.assertIn("ручная гипотеза", window.transaction_holdout_status.text())
            window.transaction_holdout_editor.setPlainText(control_sql)
            selection = window._transaction_holdout_selection
            result = self.result(window, control_sql, trial)
            window._transaction_holdout_ready((source, source, settings, "lab", comparison, selection, control_sql, result))
            self.assertIn("устойчивого подтверждения на подборе нет", window.transaction_holdout_status.text())
            self.assertIsNone(window.transaction_compare_report.preferred_variant)
        finally:
            window.close()

    def test_changed_context_clears_frozen_control_and_stale_callback_cannot_restore_it(self):
        for change in ("copy", "sql", "connection", "weights", "series"):
            window = MainWindow()
            try:
                source, control_sql, settings, comparison, trial = self.fixture(window)
                window._freeze_transaction_holdout()
                window.transaction_holdout_editor.setPlainText(control_sql)
                selection = window._transaction_holdout_selection
                result = self.result(window, control_sql, trial)
                if change == "copy":
                    window.transaction_copy_edit.setText("other")
                elif change == "sql":
                    window.transaction_editor.setPlainText("BEGIN; SELECT 1; ROLLBACK;")
                elif change == "connection":
                    window.host_edit.setText("other")
                elif change == "weights":
                    window.transaction_frequency_table.cellWidget(0, 1).setValue(20)
                else:
                    window.transaction_series_spin.setValue(3)
                self.assertIsNone(window._transaction_holdout_selection)
                self.assertFalse(window.transaction_holdout_editor.toPlainText())
                window._transaction_holdout_ready((source, source, settings, "lab", comparison, selection, control_sql, result))
                self.assertIsNone(window.transaction_holdout_report)
                self.assertIn("Устаревший", window.transaction_status.text())
            finally:
                window.close()

    def test_failure_consumes_attempt_and_does_not_claim_no_copy_commands_ran(self):
        window = MainWindow()
        try:
            self.fixture(window)
            window._freeze_transaction_holdout()
            window._transaction_holdout_attempted = True
            window._transaction_holdout_active = True
            window._transaction_failed("test error")
            self.assertFalse(window._transaction_holdout_active)
            self.assertTrue(window._transaction_holdout_attempted)
            self.assertIn("могли выполняться", window.transaction_holdout_status.text())
            self.assertFalse(window.transaction_holdout_run_button.isEnabled())
        finally:
            window.close()
