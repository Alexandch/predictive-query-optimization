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
from test_transaction_parameters import university_bindings


class TransactionParameterDesktopTests(TestCase):
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

    def fixture(self, window, *, apply=True):
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-parameters.sql").read_text(encoding="utf-8")
        window.transaction_editor.setPlainText(text)
        window._transaction_inspected((text, inspect_transaction(text)))
        if apply:
            for row, binding in enumerate(university_bindings()):
                combo = window.transaction_parameter_table.cellWidget(row, 2)
                combo.setCurrentIndex(combo.findData(binding.type_name))
                window.transaction_parameter_table.cellWidget(row, 4).setText(binding.value)
            window._apply_transaction_parameters()
        return text, window._database_settings()

    def plans(self, window):
        bound = window._transaction_binding
        return TransactionPlanReport(bound.inspection, window._database_settings().dbname,
            tuple(StatementPlan(item.number, "planned", "fixture") for item in bound.inspection.statements))

    def indices(self, window, plans):
        return build_transaction_index_report(plans.inspection, plans, {("university", "enrollments"):
            RelationIndexes("university", "enrollments", ("enrollment_id", "student_id", "offering_id", "registered_at", "status"))})

    def test_no_guessed_types_and_offline_apply_preserves_source_and_preferences(self):
        window = MainWindow()
        try:
            text, _ = self.fixture(window, apply=False)
            self.assertEqual(window.transaction_parameter_table.rowCount(), 7)
            self.assertFalse(window.transaction_plan_button.isEnabled())
            self.assertIsNone(window.transaction_parameter_table.cellWidget(0, 2).currentData())
            window._apply_transaction_parameters()
            self.assertIsNone(window._transaction_binding)
            self.assertIn("выберите тип", window.transaction_parameter_status.text())
            with patch("psycopg.connect") as connect:
                self.fixture(window)
            connect.assert_not_called()
            self.assertEqual(window.transaction_editor.toPlainText(), text)
            self.assertTrue(window.transaction_plan_button.isEnabled())
            self.assertEqual(window.transaction_views.currentIndex(), 5)
            self.assertIn("pg_catalog.int4", window.transaction_details.toPlainText())
            self.assertFalse(any("parameter" in key for key in window.preferences.allKeys()))
        finally:
            window.close()

    def test_typed_null_is_not_an_empty_text_value(self):
        window = MainWindow()
        try:
            self.fixture(window)
            row = next(i for i, item in enumerate(window._transaction_parameter_slots) if item.statement_number == 3 and item.position == 1)
            null = window.transaction_parameter_table.cellWidget(row, 3)
            value = window.transaction_parameter_table.cellWidget(row, 4)
            null.setChecked(True)
            self.assertEqual(value.text(), "")
            self.assertFalse(value.isEnabled())
            self.assertIsNone(window._transaction_binding)
            window._apply_transaction_parameters()
            self.assertIn("CAST(NULL AS pg_catalog.text)", window._transaction_binding.sql_text)
            null.setChecked(False)
            self.assertTrue(value.isEnabled())
            window._apply_transaction_parameters()
            self.assertIn("CAST('' AS pg_catalog.text)", window._transaction_binding.sql_text)
        finally:
            window.close()

    def test_planning_and_candidate_workers_receive_bound_sql_not_raw_placeholders(self):
        window = MainWindow()
        workers = []
        try:
            text, settings = self.fixture(window)
            bound = window._transaction_binding.sql_text
            plans = self.plans(window)
            indices = self.indices(window, plans)
            window._start_worker = workers.append
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_planning()
            self.assertFalse(window.transaction_parameter_table.isEnabled())
            with patch("pqo.transaction_desktop.collect_transaction_plans", return_value=plans) as collect:
                workers.pop().run()
            self.assertEqual(collect.call_args.args[0], bound)
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_indexes()
            with patch("pqo.transaction_desktop.collect_transaction_index_candidates", return_value=indices) as collect:
                workers.pop().run()
            self.assertEqual(collect.call_args.args[0], bound)
            self.assertIsNotNone(window.transaction_index_report)
            self.assertEqual(window.transaction_editor.toPlainText(), text)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_single_trial_and_comparison_workers_use_same_explicit_value_snapshot(self):
        window = MainWindow()
        workers = []
        try:
            text, settings = self.fixture(window)
            effective = window._transaction_binding.sql_text
            plans = self.plans(window)
            indices = self.indices(window, plans)
            window._transaction_planned((text, settings, effective, plans))
            window._transaction_indexes_ready((text, settings, effective, indices))
            window.transaction_copy_edit.setText("lab")
            window.transaction_copy_check.setChecked(True)
            window._start_worker = workers.append
            action = window._selected_transaction_index().action
            report = TransactionIndexTrial(action, "lab", "measured",
                (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),), (3,), (), True, True)
            with patch("pqo.transaction_trial_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_index_trial()
            with patch("pqo.transaction_trial_desktop.run_transaction_index_trial", return_value=report) as run:
                workers.pop().run()
            self.assertEqual(run.call_args.args[0], effective)
            self.assertIs(window.transaction_trial_report, report)
            for key in ("student_id", "offering_id"):
                row = next(i for i, item in enumerate(indices.candidates) if item.action.key_columns == (key, "registered_at") and not item.action.include_columns)
                window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
            comparison = TransactionIndexComparison("lab", ((1, 1), (2, 1)), (), 6, False, None, ("cancelled",), requested_series=2)
            with patch("pqo.transaction_comparison_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_comparison()
            with patch("pqo.transaction_comparison_desktop.compare_transaction_indexes", return_value=comparison) as run:
                workers.pop().run()
            self.assertEqual(run.call_args.args[0], effective)
            self.assertIsNotNone(window.transaction_compare_report)
            self.assertEqual(window.transaction_editor.toPlainText(), text)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_changed_value_invalidates_all_results_and_stale_responses_cannot_restore_them(self):
        window = MainWindow()
        try:
            text, settings = self.fixture(window)
            effective = window._transaction_binding.sql_text
            plans, indices = self.plans(window), self.indices(window, self.plans(window))
            window._transaction_planned((text, settings, effective, plans))
            window._transaction_indexes_ready((text, settings, effective, indices))
            window.transaction_parameter_table.cellWidget(0, 4).setText("17002")
            self.assertIsNone(window.transaction_plan_report)
            self.assertIsNone(window.transaction_index_report)
            self.assertIsNone(window._transaction_binding)
            self.assertFalse(window.transaction_plan_button.isEnabled())
            window._apply_transaction_parameters()
            window._transaction_planned((text, settings, effective, plans))
            window._transaction_indexes_ready((text, settings, effective, indices))
            self.assertIsNone(window.transaction_plan_report)
            self.assertIsNone(window.transaction_index_report)
            self.assertIn("Устаревшие", window.transaction_status.text())
            window.transaction_editor.setPlainText("BEGIN; SELECT 1; ROLLBACK;")
            self.assertIsNone(window._transaction_binding)
            self.assertEqual(window.transaction_parameter_table.rowCount(), 0)
        finally:
            window.close()

    def test_real_queued_planning_restores_controls_without_dropping_parameter_binding(self):
        window = MainWindow()
        try:
            text, _ = self.fixture(window)
            report = self.plans(window)
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                 patch("pqo.transaction_desktop.collect_transaction_plans", return_value=report):
                window._start_transaction_planning()
                deadline = time.monotonic() + 5
                while window._active_workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(.005)
            self.assertFalse(window._active_workers)
            self.assertTrue(window.transaction_parameter_table.isEnabled())
            self.assertIsNotNone(window._transaction_binding)
            self.assertIsNotNone(window.transaction_plan_report)
            self.assertEqual(window.transaction_editor.toPlainText(), text)
        finally:
            window.close()
