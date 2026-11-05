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
from pqo.transaction_comparison import TransactionIndexComparison
from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
from pqo.transaction_indexes import RelationIndexes, build_transaction_index_report
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport


class TransactionComparisonDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = QSettings(str(Path(directory.name) / "prefs.ini"), QSettings.Format.IniFormat)
        patcher = patch("pqo.desktop.QSettings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, window):
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        window.transaction_editor.setPlainText(text)
        settings = window._database_settings()
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, settings.dbname, tuple(
            StatementPlan(item.number, "planned", "estimated") for item in inspection.statements))
        window._transaction_inspected((text, inspection))
        window._transaction_planned((text, settings, plans))
        relation = RelationIndexes("university", "enrollments", ("enrollment_id", "student_id", "offering_id", "registered_at", "status"))
        indices = build_transaction_index_report(inspection, plans, {("university", "enrollments"): relation})
        window._transaction_indexes_ready((text, settings, indices))
        for key in ("student_id", "offering_id"):
            row = next(i for i, item in enumerate(indices.candidates)
                if item.action.key_columns == (key, "registered_at") and not item.action.include_columns)
            window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        window.transaction_copy_edit.setText("lab")
        return text, settings, window._checked_transaction_indexes()

    def report(self, actions, weights=((1, 1), (2, 1))):
        trials = tuple(TransactionIndexTrial(action, "lab", "measured",
            (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),
             TrialReading(2, (100.,)*3, (100.,)*3, (100.,)*3, False)),
            (3,), (), True, True, frequencies=weights) for action in actions)
        return TransactionIndexComparison("lab", weights, trials, 6, False, None, ("incomplete",), requested_series=2)

    def test_explicit_checkbox_copy_and_one_to_three_candidate_limit(self):
        window = MainWindow()
        try:
            self.fixture(window)
            self.assertEqual(window._transaction_frequencies(), ((1, 1), (2, 1)))
            self.assertFalse(window.transaction_compare_button.isEnabled())
            window.transaction_copy_check.setChecked(True)
            self.assertTrue(window.transaction_compare_button.isEnabled())
            eligible = [i for i, item in enumerate(window.transaction_index_report.candidates) if item.read_uses and item.status != "covered"]
            for row in eligible[:4]:
                window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
            self.assertGreater(len(window._checked_transaction_indexes()), 3)
            self.assertFalse(window.transaction_compare_button.isEnabled())
            window._start_transaction_comparison()
            self.assertIn("трёх", window.transaction_status.text())
        finally:
            window.close()

    def test_confirmation_and_gui_lock_with_real_queued_worker(self):
        window = MainWindow()
        try:
            text, settings, actions = self.fixture(window)
            window.transaction_copy_check.setChecked(True)
            report = self.report(actions)
            with patch("pqo.transaction_comparison_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.No), \
                 patch.object(window, "_start_worker") as start:
                window._start_transaction_comparison()
            start.assert_not_called()
            with patch("pqo.transaction_comparison_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                 patch("pqo.transaction_comparison_desktop.compare_transaction_indexes", return_value=report) as run:
                window._start_transaction_comparison()
                self.assertFalse(window.analyze_button.isEnabled())
                self.assertFalse(window.transaction_index_table.isEnabled())
                self.assertFalse(window.transaction_frequency_table.isEnabled())
                self.assertFalse(window.transaction_series_spin.isEnabled())
                self.assertTrue(window.transaction_cancel_button.isEnabled())
                deadline = time.monotonic() + 5
                while window._active_workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(.005)
            self.assertFalse(window._active_workers)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertTrue(window.transaction_frequency_table.isEnabled())
            self.assertTrue(window.transaction_series_spin.isEnabled())
            self.assertEqual(window.transaction_compare_table.rowCount(), 2)
            self.assertEqual(window.transaction_views.currentIndex(), 4)
            self.assertIn("НЕ ВРЕМЯ ТРАНЗАКЦИИ", window.transaction_details.toPlainText())
            self.assertEqual(run.call_args.kwargs["frequencies"], ((1, 1), (2, 1)))
            self.assertEqual(run.call_args.kwargs["series_count"], 2)
            self.assertIsNone(window.transaction_trial_report)  # No pair result leaks into the single-index tab.
            self.assertEqual(window.transaction_editor.toPlainText(), text)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_comparison_invalidates_on_frequency_selection_copy_sql_and_connection_changes(self):
        for change in ("frequency", "selection", "copy", "sql", "connection", "series"):
            with self.subTest(change=change):
                window = MainWindow()
                try:
                    text, settings, actions = self.fixture(window)
                    report = self.report(actions)
                    window._transaction_comparison_ready((text, settings, "lab", actions, ((1, 1), (2, 1)), report))
                    self.assertIsNotNone(window.transaction_compare_report)
                    if change == "frequency":
                        window.transaction_frequency_table.cellWidget(0, 1).setValue(10)
                    elif change == "selection":
                        row = next(i for i, item in enumerate(window.transaction_index_report.candidates) if item.action == actions[0])
                        window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                    elif change == "copy":
                        window.transaction_copy_edit.setText("different-lab")
                    elif change == "sql":
                        window.transaction_editor.setPlainText("BEGIN; SELECT 1; COMMIT;")
                    elif change == "series":
                        window.transaction_series_spin.setValue(3)
                    else:
                        window.host_edit.setText("different-host")
                    self.assertIsNone(window.transaction_compare_report)
                    self.assertEqual(window.transaction_compare_table.rowCount(), 0)
                finally:
                    window.close()

    def test_stale_result_cannot_restore_comparison_after_weight_change(self):
        window = MainWindow()
        try:
            text, settings, actions = self.fixture(window)
            window.transaction_frequency_table.cellWidget(0, 1).setValue(10)
            window._transaction_comparison_ready((text, settings, "lab", actions, ((1, 1), (2, 1)), self.report(actions)))
            self.assertIsNone(window.transaction_compare_report)
            self.assertIn("Устаревшее", window.transaction_status.text())
            self.assertTrue(window.analyze_button.isEnabled())
        finally:
            window.close()

    def test_series_snapshot_blocks_stale_callback_and_table_preserves_reversed_variant_number(self):
        window = MainWindow()
        try:
            text, settings, actions = self.fixture(window)
            base = self.report(actions)
            report = replace(base, trials=(base.trials[0], base.trials[1], base.trials[1], base.trials[0]),
                expected_variants=4, completed=True, preferred_variant=0, completed_series=2,
                coordinates=((1, 1), (1, 2), (2, 2), (2, 1)), series_preferences=(1, 1))
            window._transaction_comparison_ready((text, settings, "lab", actions, base.frequencies, text, 2, report))
            self.assertIs(window.transaction_compare_report, report)
            self.assertEqual(window.transaction_compare_table.item(2, 0).text(), "2")
            self.assertEqual(window.transaction_compare_table.item(2, 1).text(), "2")
            self.assertEqual(window.transaction_compare_table.item(3, 1).text(), "1")
            self.assertIn("устойчиво", window.transaction_compare_table.item(3, 6).text())
            window.transaction_compare_table.selectRow(3)
            self.assertIn("СЕРИЯ 2, ВАРИАНТ 1", window.transaction_details.toPlainText())
            self.assertIn("не независимый контрольный набор", window.transaction_details.toPlainText())
            window.transaction_series_spin.setValue(3)
            window._transaction_comparison_ready((text, settings, "lab", actions, base.frequencies, text, 2, report))
            self.assertIsNone(window.transaction_compare_report)
            self.assertIn("Устаревшее", window.transaction_status.text())
        finally:
            window.close()

    def test_writing_cte_never_gets_frequency_and_union_does(self):
        window = MainWindow()
        try:
            text = "BEGIN; WITH x AS (DELETE FROM university.enrollments RETURNING *) SELECT * FROM x; SELECT 1 UNION ALL SELECT 2; ROLLBACK;"
            window.transaction_editor.setPlainText(text)
            window._transaction_inspected((text, inspect_transaction(text)))
            self.assertEqual(window._transaction_frequencies(), ((2, 1),))
        finally:
            window.close()

    def test_failure_message_never_claims_nothing_ran_on_copy(self):
        window = MainWindow()
        try:
            self.fixture(window)
            window._transaction_comparison_active = True
            window._transaction_failed("unexpected")
            self.assertFalse(window._transaction_comparison_active)
            self.assertIn("на копии могли выполняться", window.transaction_status.text())
            self.assertNotIn("команды транзакции не выполнялись", window.transaction_status.text())
        finally:
            window.close()

    def test_pair_cannot_be_attached_as_a_single_index_result(self):
        window = MainWindow()
        try:
            text, settings, actions = self.fixture(window)
            pair = replace(self.report(actions).trials[0], additional_actions=(actions[1],))
            window._transaction_index_trial_ready((text, settings, "lab", pair))
            self.assertIsNone(window.transaction_trial_report)
            self.assertFalse(window._transaction_index_trials)
        finally:
            window.close()
