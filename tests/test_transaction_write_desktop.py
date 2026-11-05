import os
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication

from pqo.desktop import MainWindow
from pqo.transaction_comparison import TransactionIndexComparison, source_context_digest, sql_digest
from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
from pqo.transaction_indexes import ExistingIndex, RelationIndexes, build_transaction_index_report
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport
from pqo.transaction_write_pressure import assess_write_pressure

ROOT = Path(__file__).resolve().parents[1]


class TransactionWriteDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        prefs = QSettings(str(Path(temporary.name) / "prefs.ini"), QSettings.Format.IniFormat)
        prefs.setValue("db_name", "source")
        prefs.setValue("allowed_schemas", "university")
        patcher = patch("pqo.desktop.QSettings", return_value=prefs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, window):
        text = (ROOT / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        window.transaction_editor.setPlainText(text)
        settings = window._database_settings()
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, settings.dbname, tuple(StatementPlan(item.number, "planned", "fixture", [{"Plan": {
            "Node Type": "ModifyTable", "Operation": item.kind.capitalize(), "Plans": [{"Plan Rows": 12}]}}]) for item in inspection.statements))
        relation = RelationIndexes("university", "enrollments", ("enrollment_id", "student_id", "offering_id", "registered_at", "status"),
            (ExistingIndex("pk", ("enrollment_id",)),), write_metadata_known=True, relation_kind="r")
        indices = build_transaction_index_report(inspection, plans, {("university", "enrollments"): relation})
        window._transaction_inspected((text, inspection))
        window._transaction_planned((text, settings, plans))
        window._transaction_indexes_ready((text, settings, indices))
        window.transaction_write_source.setCurrentIndex(1)
        candidate_row = next(i for i, item in enumerate(indices.candidates) if item.action.key_columns == ("student_id",) and "status" in item.action.include_columns)
        window.transaction_index_table.item(candidate_row, 0).setCheckState(Qt.CheckState.Checked)
        window.transaction_views.setCurrentIndex(7)
        return text, settings, plans, indices, indices.candidates[candidate_row].action

    def drain(self, window):
        deadline = time.monotonic() + 5
        while window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(window._active_workers)

    def test_unknown_defaults_and_real_worker_without_sql_connection(self):
        window = MainWindow()
        try:
            text, settings, plans, indices, action = self.fixture(window)
            self.assertEqual(window.transaction_write_table.rowCount(), 3)
            self.assertTrue(all(window.transaction_write_table.cellWidget(row, 4).text() == "" for row in range(3)))
            window.transaction_write_budget.setText("0")
            with patch("psycopg.connect") as connect:
                window._start_transaction_write()
                self.assertFalse(window.analyze_button.isEnabled())
                self.assertFalse(window.transaction_write_table.isEnabled())
                self.assertTrue(window.transaction_cancel_button.isEnabled())
                self.drain(window)
            connect.assert_not_called()
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertEqual(window.transaction_write_report.unknown_commands, (3, 4, 5))
            self.assertEqual(window.transaction_write_report.budget_state, "unknown")
            self.assertIn("Чистый выигрыш транзакции не вычислен", window.transaction_details.toPlainText())
            self.assertEqual(window.transaction_editor.toPlainText(), text)
            self.assertIs(window.transaction_index_report, indices)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_explicit_assumptions_compute_budget_and_edits_invalidate_only_write_result(self):
        window = MainWindow()
        try:
            self.fixture(window)
            for row, repetitions, rows in ((0, 10, "100"), (1, 50, "1"), (2, 2, "100")):
                window.transaction_write_table.cellWidget(row, 3).setValue(repetitions)
                window.transaction_write_table.cellWidget(row, 4).setText(rows)
            window.transaction_write_budget.setText("1000")
            window._start_transaction_write()
            self.drain(window)
            self.assertEqual(window.transaction_write_report.known_exposure, 1250)
            self.assertEqual(window.transaction_write_report.budget_state, "exceeded")
            window.transaction_write_table.cellWidget(0, 4).setText("0")
            self.assertIsNone(window.transaction_write_report)
            self.assertIsNotNone(window.transaction_index_report)
            self.assertIsNotNone(window.transaction_plan_report)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_stale_callback_after_context_or_assumption_change_is_not_attached(self):
        for change in ("rows", "sql", "connection", "selection", "budget"):
            window = MainWindow()
            try:
                text, settings, plans, indices, action = self.fixture(window)
                assumptions = window._write_assumptions_input()
                report = assess_write_pressure(text, plans, indices, (action,), assumptions=assumptions)
                if change == "rows":
                    window.transaction_write_table.cellWidget(0, 4).setText("10")
                elif change == "sql":
                    window.transaction_editor.setPlainText("BEGIN; SELECT 1; ROLLBACK;")
                elif change == "connection":
                    window.host_edit.setText("another")
                elif change == "budget":
                    window.transaction_write_budget.setText("100")
                else:
                    row = next(i for i, item in enumerate(indices.candidates) if item.action == action)
                    window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                window._transaction_write_ready((text, text, settings, plans, indices, (action,), assumptions, None, "checked", None, report))
                self.assertIsNone(window.transaction_write_report)
                self.assertIn("Устаревшая", window.transaction_status.text())
            finally:
                window.close()

    def test_measured_reading_is_shown_separately_and_never_subtracted_from_proxy(self):
        window = MainWindow()
        try:
            text, settings, plans, indices, action = self.fixture(window)
            trial = TransactionIndexTrial(action, "lab", "measured", tuple(TrialReading(n, (10.,)*3, (1.,)*3, (10.,)*3, True) for n in (1, 2)),
                (3, 4, 5), (), True, True, frequencies=((1, 1), (2, 1)))
            comparison = TransactionIndexComparison("lab", trial.frequencies, (trial, trial), 2, True, 0, (),
                requested_series=2, completed_series=2, coordinates=((1, 1), (2, 1)), series_preferences=(1, 1),
                development_sql_digest=sql_digest(text), source_context_digest=source_context_digest(settings))
            window.transaction_copy_edit.setText("lab")
            window._transaction_comparison_ready((text, settings, "lab", (action,), trial.frequencies, text, 2, comparison))
            window.transaction_write_source.setCurrentIndex(0)
            window._start_transaction_write()
            self.drain(window)
            self.assertIsNotNone(window.transaction_write_report)
            self.assertEqual(window.transaction_write_report.read_trial.weighted_gain_ms, 18)
            self.assertIsNone(window.transaction_write_report.net_benefit_ms)
            self.assertIn("разные единицы и не вычитаются", window.transaction_details.toPlainText())
            self.assertIs(window.transaction_compare_report, comparison)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_dml_only_checkbox_is_available_for_write_scenario_but_not_read_comparison(self):
        window = MainWindow()
        try:
            text, settings, plans, indices, action = self.fixture(window)
            for row in range(window.transaction_index_table.rowCount()):
                window.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
            row = next(i for i, item in enumerate(indices.candidates) if item.action.key_columns == ("offering_id",) and not item.action.include_columns)
            cell = window.transaction_index_table.item(row, 0)
            self.assertTrue(cell.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            cell.setCheckState(Qt.CheckState.Checked)
            self.assertTrue(window.transaction_write_button.isEnabled())
            self.assertFalse(window._checked_transaction_indexes())
            self.assertFalse(window.transaction_compare_button.isEnabled())
        finally:
            window.close()
