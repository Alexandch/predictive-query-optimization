import os
from pathlib import Path
import unittest
import threading
import time
import tempfile
from unittest.mock import patch
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pqo.desktop import (
    DEFAULT_DQN_MODEL,
    DEFAULT_DQN_STRESS_METRICS,
    DEFAULT_SEQUENTIAL_DQN_MODEL,
    DEFAULT_STRATEGY_MODEL,
    DEFAULT_XGB_MODEL,
    MainWindow,
    Worker,
    _format_index_action,
    _format_index_impact,
    _format_index_candidate_checks,
    _format_prediction,
    _format_sql_rewrite_diff,
    _format_structural_recommendations,
    _sql_editor_identity,
)
from pqo.analysis_service import QueryAnalysis, analyze_query
from pqo.index_actions import IndexAction
from pqo.prediction import QueryTimePrediction
from pqo.recommendation import IndexRecommendation
from pqo.sequential_recommendation import SequentialRecommendationStep
from pqo.sequential_recommendation import SequentialRecommendationPlan, SequentialCandidateCheck
from pqo.history import (
    HistoryRecord,
    HistorySequentialStep,
    HistoryStructuralFeedback,
)
from pqo.structural_advisor import (
    RecommendationCategory,
    RecommendationPriority,
    StructuralRecommendation,
)
from pqo.sql_rewrite import (
    SQLRewriteCandidate,
    SQLRewriteEvaluation,
    SQLRewritePlan,
)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtCore import QSettings
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = QSettings(str(Path(directory.name) / "preferences.ini"), QSettings.Format.IniFormat)
        patcher = patch("pqo.desktop.QSettings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidate_choice_plan(self):
        original = "SELECT 1 AS value"
        evaluations = (
            SQLRewriteEvaluation(SQLRewriteCandidate("fast", "Быстрый вариант", "SELECT 1::int AS value"),
                                 True, 100., 40., .6, True, None),
            SQLRewriteEvaluation(SQLRewriteCandidate("slow", "Медленный вариант", "SELECT CAST(1 AS bigint) AS value"),
                                 True, 100., 110., -.1, False, "improvement_too_small"),
            SQLRewriteEvaluation(SQLRewriteCandidate("mismatch", "Другой результат", "SELECT 2 AS value"),
                                 False, None, None, None, False, "result_mismatch"),
        )
        return SQLRewritePlan(original, evaluations, evaluations[0], 5., 5., .05, "recommended")

    def test_rewrite_candidate_selector_preserves_source_and_rejects_unsafe_apply(self):
        window = MainWindow()
        try:
            plan = self.candidate_choice_plan()
            window.sql_editor.setPlainText(plan.original_sql)
            window._show_rewrite_analysis(plan)
            self.assertEqual(window.rewrite_candidate_combo.count(), 3)
            self.assertTrue(window.apply_rewrite_button.isEnabled())
            self.assertIn("100.00 → 40.00", window.rewrite_candidate_details.toPlainText())
            window.rewrite_candidate_combo.setCurrentIndex(1)
            self.assertEqual(window.rewrite_editor.toPlainText(), plan.evaluations[1].candidate.sql_text)
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            self.assertIn("Недостаточное ускорение", window.rewrite_candidate_details.toPlainText())
            window.rewrite_candidate_combo.setCurrentIndex(2)
            self.assertIn("Результаты различаются", window.rewrite_candidate_details.toPlainText())
            self.assertIn("замеры отсутствуют", window.rewrite_candidate_details.toPlainText())
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            window.rewrite_candidate_combo.setCurrentIndex(0)
            self.assertTrue(window.apply_rewrite_button.isEnabled())
            self.assertEqual(window.sql_editor.toPlainText(), plan.original_sql)
        finally:
            window.close()

    def test_rewrite_choices_cleared_on_source_edit_and_disabled_during_work(self):
        window = MainWindow()
        try:
            plan = self.candidate_choice_plan()
            window.sql_editor.setPlainText(plan.original_sql)
            window._show_rewrite_analysis(plan)
            window._set_query_actions_enabled(False)
            self.assertFalse(window.rewrite_candidate_combo.isEnabled())
            window._select_rewrite_candidate(2)
            self.assertEqual(window.rewrite_editor.toPlainText(), plan.recommended.candidate.sql_text)
            window._set_query_actions_enabled(True)
            window.rewrite_editor.setPlainText("SELECT 3 AS value")
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            self.assertEqual(window.rewrite_candidate_details.toPlainText(), "")
            window.sql_editor.setPlainText("SELECT 4 AS value")
            self.assertEqual(window.rewrite_candidate_combo.count(), 0)
            self.assertFalse(window.rewrite_candidate_combo.isEnabled())
            self.assertFalse(window.apply_rewrite_button.isEnabled())
        finally:
            window.close()

    def test_deep_result_shows_retries_without_enabling_rejected_plan(self):
        action = IndexAction.create("aviation", "flights", ("status",))
        checks = (SequentialCandidateCheck(action, 1, .8, False, "index_not_used", 100., 110.),)
        plan = SequentialRecommendationPlan((), 100., 100., 10000, 0, 2, .1, 0., 5., "index_not_used",
                                             candidate_checks=checks)
        window = MainWindow()
        try:
            window._show_sequential_analysis(plan)
            text = window.recommendation_text.toPlainText()
            self.assertIn("Фактически проверено кандидатов: 1", text)
            self.assertIn("100.00 → 110.00", text)
            self.assertFalse(window.apply_indexes_button.isEnabled())
        finally:
            window.close()

    def test_candidate_trial_summary_distinguishes_rejected_accepted_and_missing_times(self):
        actions = [IndexAction.create("aviation", "flights", (key,)) for key in ("status", "flight_id")]
        checks = (
            SequentialCandidateCheck(actions[0], 1, .8, False, "non_positive_reward", 100., 120.),
            SequentialCandidateCheck(actions[1], 1, .7, True, None, 100., 20.),
            SequentialCandidateCheck(actions[0], 2, .6, False, "statement_timeout", 20.),
        )
        plan = SequentialRecommendationPlan((), 100., 20., 10000, 100, 2, .1, 0., 5., "max_steps",
                                             candidate_checks=checks)
        text = _format_index_candidate_checks(plan)
        self.assertIn("Фактически проверено кандидатов: 3", text)
        self.assertIn("100.00 → 120.00", text)
        self.assertIn("принят в проверенный план", text)
        self.assertIn("по тайм-ауту", text)
        self.assertIn("не полный перебор", text)
        self.assertIn("замер с индексом не завершён", text)

    def test_no_candidates_are_submitted_to_the_journal_without_executing_sql(self):
        window = MainWindow()
        try:
            window.rewrite_journal_check.blockSignals(True)
            window.rewrite_journal_check.setChecked(True)
            window.sql_editor.setPlainText("SELECT 1")
            window._start_worker = lambda worker: None
            with patch("pqo.desktop.Worker") as factory:
                window._start_rewrite_analysis()
            self.assertIn("journal_directory", factory.call_args.kwargs)
            self.assertEqual(factory.call_args.kwargs["journal_source"], "desktop")
            self.assertTrue(window._query_operation_active)
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_editing_right_sql_requires_reverification_and_keeps_left(self):
        window = MainWindow()
        try:
            original = "SELECT * FROM (SELECT 1 AS x ORDER BY x) q"
            rewritten = "SELECT * FROM (SELECT 1 AS x) q"
            window.sql_editor.setPlainText(original)
            window._set_verified_rewrite(original, rewritten, "Проверено")
            self.assertTrue(window.apply_rewrite_button.isEnabled())
            window.rewrite_editor.setPlainText("SELECT 2 AS x")
            self.assertEqual(window.sql_editor.toPlainText(), original)
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            self.assertTrue(window.compare_variant_button.isEnabled())
            window._set_query_actions_enabled(False)
            self.assertFalse(window.compare_variant_button.isEnabled())
            self.assertTrue(window.rewrite_editor.isReadOnly())
            self.assertTrue(window.sql_editor.isReadOnly())
            window._set_query_actions_enabled(True)
        finally:
            window.close()

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_workers_use_python_threads_and_release_after_queued_completion(self):
        window = MainWindow()
        observed = []
        worker = Worker(lambda: observed.append(threading.current_thread().name))
        try:
            window._start_worker(worker)
            deadline = time.monotonic() + 5
            while window._active_workers and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertEqual(observed, ["pqo-worker"])
            self.assertFalse(window._active_workers)
        finally:
            window.close()

    def test_main_window_contains_complete_workflow(self):
        window = MainWindow()
        try:
            self.assertEqual(window.tabs.count(), 6)
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(window.tabs.count())],
                ["Анализ", "Транзакции", "История", "Эксперименты", "Настройки", "Обучение"],
            )
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertEqual(
                window.analyze_button.text(), "Быстрый ML-прогноз"
            )
            self.assertIn("SELECT", window.sql_editor.toPlainText())
            self.assertIsNotNone(window.persist_check)
            self.assertTrue(window.calibrate_button.isHidden())
            self.assertTrue(window.batch_calibrate_button.isEnabled())
            self.assertTrue(window.deep_analyze_button.isEnabled())
            self.assertTrue(window.rewrite_analyze_button.isEnabled())
            self.assertEqual(
                window.rewrite_analyze_button.text(),
                "Проверить безопасное переписывание SQL",
            )
            self.assertTrue(window.analysis_scroll.widgetResizable())
            self.assertEqual(window.sql_splitter.count(), 2)
            self.assertEqual(window.rewrite_journal_table.columnCount(), 8)
            self.assertFalse(window.compare_variant_button.isEnabled())
            self.assertFalse(window.apply_indexes_button.isEnabled())
            self.assertFalse(window.rollback_indexes_button.isEnabled())
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            self.assertFalse(window.rollback_rewrite_button.isEnabled())
            self.assertFalse(window.accept_structural_button.isEnabled())
            self.assertFalse(window.reject_structural_button.isEnabled())
            self.assertFalse(window.structural_before_spin.isEnabled())
            self.assertFalse(window.structural_after_spin.isEnabled())
            self.assertFalse(window.save_structural_measurement_button.isEnabled())
            self.assertFalse(window.validate_structural_button.isEnabled())
            self.assertEqual(
                Path(window.strategy_model_edit.text()), DEFAULT_STRATEGY_MODEL
            )
            self.assertTrue(window.use_calibration_check.isChecked())
            self.assertTrue(window.use_calibration_check.isHidden())
            self.assertGreaterEqual(window.minimum_gain_spin.value(), 0)
            self.assertGreaterEqual(window.minimum_gain_percent_spin.value(), 0)
        finally:
            window.close()

    def test_transaction_inventory_is_separate_and_locks_conflicting_actions(self):
        window = MainWindow()
        captured = []
        try:
            original = window.sql_editor.toPlainText()
            window.transaction_editor.setPlainText("BEGIN; SELECT 1; UPDATE university.students SET campus_id=7; ROLLBACK;")
            window._start_worker = captured.append
            window._start_transaction_inspection()
            self.assertTrue(window.transaction_editor.isReadOnly())
            self.assertFalse(window.analyze_button.isEnabled())
            self.assertFalse(window.deep_analyze_button.isEnabled())
            self.assertFalse(window.transaction_inspect_button.isEnabled())
            captured[0].run()
            self.assertEqual(window.transaction_table.rowCount(), 2)
            self.assertEqual(window.transaction_table.item(1, 1).text(), "UPDATE")
            self.assertTrue(window.transaction_plan_button.isEnabled())
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertEqual(window.sql_editor.toPlainText(), original)
            self.assertFalse(window.transaction_inspection.execution_permitted)
            window.transaction_editor.setPlainText("BEGIN; SELECT 2; COMMIT;")
            self.assertIsNone(window.transaction_inspection)
            self.assertFalse(window.transaction_plan_button.isEnabled())
            self.assertEqual(window.transaction_table.rowCount(), 0)
        finally:
            window.close()

    def test_transaction_plans_cleared_after_connection_edit_and_stale_results_ignored(self):
        from pqo.transaction_inspection import inspect_transaction
        from pqo.transaction_planning import StatementPlan, TransactionPlanReport
        window = MainWindow()
        try:
            text = window.transaction_editor.toPlainText()
            settings = window._database_settings()
            inspection = inspect_transaction(text)
            window._transaction_inspected((text, inspection))
            report = TransactionPlanReport(inspection, settings.dbname, (
                StatementPlan(1, "planned", "plan only", [{"Plan": {"Node Type": "Result", "Total Cost": 12}}]),
            ))
            window._transaction_planned((text, settings, report))
            self.assertEqual(window.transaction_table.item(0, 6).text(), "12.00")
            self.assertIn("plan only", window.transaction_details.toPlainText())
            window.database_edit.setText("different_database")
            self.assertIsNone(window.transaction_plan_report)
            self.assertEqual(window.transaction_table.item(0, 6).text(), "—")
            self.assertIsNotNone(window.transaction_inspection)
            window._transaction_planned((text, settings, report))
            self.assertIsNone(window.transaction_plan_report)
            self.assertIn("Устаревшие", window.transaction_status.text())
        finally:
            window.close()

    def test_transaction_planning_requires_confirmation_and_recovers_after_cancel(self):
        from PyQt6.QtWidgets import QMessageBox
        from pqo.transaction_inspection import inspect_transaction
        window = MainWindow()
        workers = []
        try:
            text = window.transaction_editor.toPlainText()
            window._transaction_inspected((text, inspect_transaction(text)))
            window._start_worker = workers.append
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                window._start_transaction_planning()
            self.assertFalse(workers)
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_planning()
            self.assertEqual(len(workers), 1)
            self.assertTrue(window.transaction_cancel_button.isEnabled())
            self.assertFalse(window.analyze_button.isEnabled())
            window._start_transaction_inspection()
            self.assertEqual(len(workers), 1)
            window._cancel_transaction_planning()
            self.assertTrue(window._transaction_control.cancelled.is_set())
            self.assertFalse(window.transaction_cancel_button.isEnabled())
            window._transaction_failed("AnalysisCancelled")
            self.assertFalse(window._query_operation_active)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertTrue(window.transaction_plan_button.isEnabled())
            self.assertIn("отмен", window.transaction_status.text())
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_transaction_parse_error_restores_controls_without_stale_inventory(self):
        window = MainWindow()
        try:
            window.transaction_editor.setPlainText("SELECT 1")
            window._set_query_actions_enabled(False)
            window._transaction_failed("Expected BEGIN")
            self.assertTrue(window.transaction_inspect_button.isEnabled())
            self.assertFalse(window.transaction_plan_button.isEnabled())
            self.assertFalse(window.transaction_editor.isReadOnly())
            self.assertIn("Expected BEGIN", window.transaction_status.text())
        finally:
            window.close()

    def test_transaction_inventory_worker_returns_to_gui_and_releases_controls(self):
        window = MainWindow()
        try:
            window.transaction_editor.setPlainText("BEGIN; SELECT 1; SELECT 2; ROLLBACK;")
            window._start_transaction_inspection()
            deadline = time.monotonic() + 5
            while window._active_workers and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertFalse(window._active_workers)
            self.assertFalse(window._query_operation_active)
            self.assertEqual(window.transaction_table.rowCount(), 2)
            self.assertTrue(window.transaction_plan_button.isEnabled())
            self.assertFalse(window.transaction_editor.isReadOnly())
            self.assertTrue(window.analyze_button.isEnabled())
        finally:
            window.close()

    def transaction_index_fixture(self, window):
        from pqo.transaction_inspection import inspect_transaction
        from pqo.transaction_planning import StatementPlan, TransactionPlanReport
        from pqo.transaction_indexes import build_transaction_index_report, ExistingIndex, RelationIndexes
        text = "BEGIN; SELECT campus_id FROM university.students WHERE student_id=1; UPDATE university.students SET campus_id=7 WHERE student_id=1; ROLLBACK;"
        window.transaction_editor.setPlainText(text)
        settings = window._database_settings()
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, settings.dbname, tuple(
            StatementPlan(item.number, "planned", "estimated", [{"Plan": {"Node Type": "Result", "Total Cost": 1}}])
            for item in inspection.statements))
        window._transaction_inspected((text, inspection))
        window._transaction_planned((text, settings, plans))
        relation = RelationIndexes("university", "students", ("student_id", "campus_id"),
                                   (ExistingIndex("students_pkey", ("student_id",)),))
        report = build_transaction_index_report(inspection, plans, {("university", "students"): relation})
        return text, settings, report

    def test_transaction_index_table_shows_existing_indexes_and_include_write_risk(self):
        window = MainWindow()
        try:
            text, settings, report = self.transaction_index_fixture(window)
            self.assertTrue(window.transaction_index_button.isEnabled())
            window._transaction_indexes_ready((text, settings, report))
            self.assertEqual(window.transaction_views.currentIndex(), 1)
            self.assertEqual(window.transaction_index_table.rowCount(), 2)
            details = window.transaction_details.toPlainText()
            self.assertIn("CREATE INDEX", details)
            self.assertIn("campus_id", details)
            self.assertIn("HOT", details)
            self.assertIn("только INCLUDE", window.transaction_index_table.item(0, 5).text())
            window.transaction_index_table.selectRow(1)
            self.assertIn("Новый дубликат не предлагается", window.transaction_details.toPlainText())
            self.assertEqual(window.transaction_editor.toPlainText(), text)
            window.transaction_views.setCurrentIndex(0)
            self.assertIn("Команда 1", window.transaction_details.toPlainText())
            window.database_edit.setText("changed_database")
            self.assertIsNone(window.transaction_index_report)
            self.assertEqual(window.transaction_index_table.rowCount(), 0)
            self.assertFalse(window.transaction_index_button.isEnabled())
        finally:
            window.close()

    def test_transaction_index_worker_requires_confirmation_and_locks_all_analysis(self):
        from PyQt6.QtWidgets import QMessageBox
        window = MainWindow()
        workers = []
        try:
            text, settings, report = self.transaction_index_fixture(window)
            window._start_worker = workers.append
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                window._start_transaction_indexes()
            self.assertFalse(workers)
            with patch("pqo.transaction_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_indexes()
            self.assertEqual(len(workers), 1)
            self.assertFalse(window.transaction_index_button.isEnabled())
            self.assertFalse(window.analyze_button.isEnabled())
            self.assertFalse(window.transaction_plan_button.isEnabled())
            self.assertTrue(window.transaction_cancel_button.isEnabled())
            with patch("pqo.transaction_desktop.collect_transaction_index_candidates", return_value=report):
                workers[0].run()
            self.assertIsNotNone(window.transaction_index_report)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertTrue(window.transaction_index_button.isEnabled())
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_stale_transaction_index_result_does_not_restore_removed_report(self):
        window = MainWindow()
        try:
            text, settings, report = self.transaction_index_fixture(window)
            window.transaction_editor.setPlainText("BEGIN; SELECT 1; ROLLBACK;")
            window._transaction_indexes_ready((text, settings, report))
            self.assertIsNone(window.transaction_index_report)
            self.assertEqual(window.transaction_index_table.rowCount(), 0)
            self.assertIn("Устаревшие", window.transaction_status.text())
        finally:
            window.close()

    def test_transaction_trial_requires_distinct_copy_and_explicit_confirmation(self):
        from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
        from PyQt6.QtWidgets import QMessageBox
        window = MainWindow()
        workers = []
        try:
            text, settings, indices = self.transaction_index_fixture(window)
            window._transaction_indexes_ready((text, settings, indices))
            window._start_worker = workers.append
            window.transaction_copy_edit.setText(settings.dbname)
            window.transaction_copy_check.setChecked(True)
            self.assertFalse(window.transaction_trial_button.isEnabled())
            window._start_transaction_index_trial()
            self.assertFalse(workers)
            window.transaction_copy_edit.setText("separate_lab")
            self.assertFalse(window.transaction_copy_check.isChecked())
            window.transaction_copy_check.setChecked(True)
            self.assertTrue(window.transaction_trial_button.isEnabled())
            report = TransactionIndexTrial(indices.candidates[0].action, "separate_lab", "measured",
                (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),), (2,), (), True, True)
            with patch("pqo.transaction_trial_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_index_trial()
            self.assertEqual(len(workers), 1)
            self.assertFalse(window.analyze_button.isEnabled())
            self.assertFalse(window.transaction_trial_button.isEnabled())
            self.assertTrue(window.transaction_copy_edit.isReadOnly())
            self.assertTrue(window.transaction_cancel_button.isEnabled())
            with patch("pqo.transaction_trial_desktop.run_transaction_index_trial", return_value=report):
                workers[0].run()
            self.assertEqual(window.transaction_views.currentIndex(), 2)
            self.assertEqual(window.transaction_trial_table.rowCount(), 1)
            self.assertIn("100.000 → 10.000", window.transaction_details.toPlainText())
            self.assertIn("не время транзакции", window.transaction_details.toPlainText())
            self.assertTrue(window.analyze_button.isEnabled())
            window.transaction_copy_edit.setText("different_lab")
            self.assertIsNone(window.transaction_trial_report)
            self.assertFalse(window.transaction_trial_button.isEnabled())
        finally:
            window._set_query_actions_enabled(True)
            window.close()

    def test_trial_result_stale_or_cancelled_never_misreports_source_commands_as_not_run(self):
        from pqo.transaction_index_trial import TransactionIndexTrial
        window = MainWindow()
        try:
            text, settings, indices = self.transaction_index_fixture(window)
            window._transaction_indexes_ready((text, settings, indices))
            report = TransactionIndexTrial(indices.candidates[0].action, "lab", "cancelled", (), (2,), ("cancelled",), False, True)
            window.transaction_copy_edit.setText("other_lab")
            window._transaction_index_trial_ready((text, settings, "lab", report))
            self.assertIsNone(window.transaction_trial_report)
            self.assertIn("отсутствие индекса подтверждено", window.transaction_status.text())
            window._transaction_trial_active = True
            window._transaction_failed("unexpected")
            self.assertIn("SELECT на копии могли выполняться", window.transaction_status.text())
            self.assertNotIn("команды транзакции не выполнялись", window.transaction_status.text())
            self.assertTrue(window.analyze_button.isEnabled())
        finally:
            window.close()

    def test_trial_unverified_rollback_is_visible_and_never_shows_confirmed_optimization(self):
        from pqo.transaction_index_trial import TransactionIndexTrial
        window = MainWindow()
        try:
            text, settings, indices = self.transaction_index_fixture(window)
            window._transaction_indexes_ready((text, settings, indices))
            window.transaction_copy_edit.setText("lab")
            report = TransactionIndexTrial(indices.candidates[0].action, "lab", "error", (), (2,), ("rollback unverified",), False, False)
            window._transaction_index_trial_ready((text, settings, "lab", report))
            self.assertIn("ОТКАТ НЕ ПОДТВЕРЖДЁН", window.transaction_status.text())
            self.assertIn("не подтверждена", window.transaction_details.toPlainText())
            self.assertTrue(window.analyze_button.isEnabled())
        finally:
            window.close()

    def test_transaction_trial_worker_releases_gui_controls_after_queued_result(self):
        from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
        from PyQt6.QtWidgets import QMessageBox
        window = MainWindow()
        try:
            text, settings, indices = self.transaction_index_fixture(window)
            window._transaction_indexes_ready((text, settings, indices))
            window.transaction_copy_edit.setText("lab")
            window.transaction_copy_check.setChecked(True)
            report = TransactionIndexTrial(indices.candidates[0].action, "lab", "measured",
                (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),), (2,), (), True, True)
            with patch("pqo.transaction_trial_desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                 patch("pqo.transaction_trial_desktop.run_transaction_index_trial", return_value=report):
                window._start_transaction_index_trial()
                deadline = time.monotonic() + 5
                while window._active_workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(.005)
            self.assertFalse(window._active_workers)
            self.assertFalse(window._query_operation_active)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertFalse(window.transaction_copy_edit.isReadOnly())
            self.assertEqual(window.transaction_trial_table.rowCount(), 1)
        finally:
            window.close()

    def transaction_shared_fixture(self, window):
        from pqo.transaction_inspection import inspect_transaction
        from pqo.transaction_planning import StatementPlan, TransactionPlanReport
        from pqo.transaction_indexes import build_transaction_index_report, RelationIndexes
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        window.transaction_editor.setPlainText(text)
        settings = window._database_settings()
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, settings.dbname, tuple(
            StatementPlan(item.number, "planned", "estimated") for item in inspection.statements))
        window._transaction_inspected((text, inspection))
        window._transaction_planned((text, settings, plans))
        relation = RelationIndexes("university", "enrollments", ("enrollment_id", "student_id", "offering_id", "status", "registered_at"))
        indices = build_transaction_index_report(inspection, plans, {("university", "enrollments"): relation})
        window._transaction_indexes_ready((text, settings, indices))
        window.transaction_copy_edit.setText("lab")
        return text, settings, indices

    def test_dml_lookup_candidate_cannot_start_select_trial_even_by_direct_slot(self):
        window = MainWindow()
        try:
            _, _, indices = self.transaction_shared_fixture(window)
            row = next(i for i, c in enumerate(indices.candidates) if c.action.key_columns == ("offering_id",) and not c.action.include_columns)
            window.transaction_index_table.selectRow(row)
            window.transaction_copy_check.setChecked(True)
            self.assertEqual(window.transaction_index_table.item(row, 2).text(), "—")
            self.assertEqual(window.transaction_index_table.item(row, 3).text(), "3")
            self.assertEqual(window.transaction_index_table.item(row, 5).text(), "только поиск в DML")
            self.assertFalse(window.transaction_trial_button.isEnabled())
            self.assertIn("нет применения", window.transaction_trial_button.toolTip())
            self.assertIn("не означает, что индекс бесполезен", window.transaction_details.toPlainText())
            with patch.object(window, "_start_worker") as worker, patch("pqo.transaction_trial_desktop.QMessageBox.question") as confirm:
                window._start_transaction_index_trial()
            worker.assert_not_called()
            confirm.assert_not_called()
        finally:
            window.close()

    def test_each_index_keeps_own_trial_and_card_never_shows_other_candidate_result(self):
        from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
        window = MainWindow()
        try:
            text, settings, indices = self.transaction_shared_fixture(window)
            rows = [i for i, c in enumerate(indices.candidates) if c.read_uses][:2]
            first, second = [indices.candidates[i].action for i in rows]
            positive = TransactionIndexTrial(first, "lab", "measured",
                (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),), (3, 4, 5), (), True, True)
            negative = TransactionIndexTrial(second, "lab", "measured",
                (TrialReading(1, (100.,)*3, (101.,)*3, (100.,)*3, False),), (3, 4, 5), ("unused index",), False, True)
            window._transaction_index_trial_ready((text, settings, "lab", positive))
            window._transaction_index_trial_ready((text, settings, "lab", negative))
            self.assertEqual(window.transaction_index_table.item(rows[0], 5).text(), "чтение: ускорение")
            self.assertEqual(window.transaction_index_table.item(rows[1], 5).text(), "чтение: не подтверждено")
            window.transaction_views.setCurrentIndex(1)
            window.transaction_index_table.selectRow(rows[0])
            self.assertIn("100.000 → 10.000", window.transaction_details.toPlainText())
            self.assertNotIn("НЕ ПРОВЕРЕН УСКОРЕНИЕМ", window.transaction_details.toPlainText())
            self.assertIn("Польза и цена записи не измерялись", window.transaction_details.toPlainText())
            window.transaction_views.setCurrentIndex(2)
            self.assertIs(window.transaction_trial_report, positive)
            self.assertIn("100.000 → 10.000", window.transaction_details.toPlainText())
            window.transaction_views.setCurrentIndex(1)
            window.transaction_index_table.selectRow(rows[1])
            self.assertIn("unused index", window.transaction_details.toPlainText())
            self.assertNotIn("100.000 → 10.000", window.transaction_details.toPlainText())
        finally:
            window.close()

    def test_trial_evidence_invalidates_on_sql_connection_copy_and_candidate_refresh(self):
        from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
        for change in ("sql", "connection", "copy", "refresh"):
            with self.subTest(change=change):
                window = MainWindow()
                try:
                    text, settings, indices = self.transaction_shared_fixture(window)
                    report = TransactionIndexTrial(indices.candidates[0].action, "lab", "measured",
                        (TrialReading(1, (100.,)*3, (10.,)*3, (100.,)*3, True),), (3, 4, 5), (), True, True)
                    window._transaction_index_trial_ready((text, settings, "lab", report))
                    self.assertTrue(window._transaction_index_trials)
                    if change == "sql":
                        window.transaction_editor.setPlainText("BEGIN; SELECT 1; COMMIT;")
                    elif change == "connection":
                        window.host_edit.setText("different-host")
                    elif change == "copy":
                        window.transaction_copy_edit.setText("other-lab")
                    else:
                        window._transaction_indexes_ready((text, settings, indices))
                    self.assertFalse(window._transaction_index_trials)
                    self.assertIsNone(window.transaction_trial_report)
                    self.assertFalse(any(window.transaction_index_table.item(row, 5).text() == "чтение: ускорение"
                        for row in range(window.transaction_index_table.rowCount())))
                finally:
                    window.close()

    def test_cancelled_trial_is_attached_to_candidate_without_acceleration_claim(self):
        from pqo.transaction_index_trial import TransactionIndexTrial
        window = MainWindow()
        try:
            text, settings, indices = self.transaction_shared_fixture(window)
            report = TransactionIndexTrial(indices.candidates[0].action, "lab", "cancelled", (), (3, 4, 5), ("cancelled",), False, True)
            window._transaction_index_trial_ready((text, settings, "lab", report))
            self.assertEqual(window.transaction_index_table.item(0, 5).text(), "проба отменена")
            window.transaction_views.setCurrentIndex(1)
            self.assertIn("проба отменена", window.transaction_details.toPlainText())
            self.assertNotIn("НЕ ПРОВЕРЕН УСКОРЕНИЕМ", window.transaction_details.toPlainText())
        finally:
            window.close()

    def test_verified_rewrite_can_be_applied_and_rolled_back(self):
        window = MainWindow()
        try:
            original = "SELECT * FROM (SELECT id FROM aviation.flights ORDER BY id) q;"
            normalized_original = original.removesuffix(";")
            rewritten = "SELECT * FROM (SELECT id FROM aviation.flights) q"
            window.sql_editor.setPlainText(original)
            window._set_verified_rewrite(normalized_original, rewritten, "Проверено.")
            self.assertEqual(window.rewrite_editor.toPlainText(), rewritten)
            window._start_rewrite_control_measurement = lambda: None

            self.assertTrue(window.apply_rewrite_button.isEnabled())
            window._apply_verified_rewrite()
            self.assertEqual(window.sql_editor.toPlainText(), rewritten)
            self.assertTrue(window.rollback_rewrite_button.isEnabled())

            window._rollback_applied_rewrite()
            self.assertEqual(window.sql_editor.toPlainText(), original)
            self.assertFalse(window.rollback_rewrite_button.isEnabled())
            self.assertTrue(window.apply_rewrite_button.isEnabled())
        finally:
            window.close()

    def test_query_operation_disables_analysis_and_rewrite_controls(self):
        window = MainWindow()
        try:
            original = "SELECT * FROM aviation.flights;"
            rewritten = "SELECT flight_id FROM aviation.flights"
            window.sql_editor.setPlainText(original)
            window._set_verified_rewrite(original, rewritten, "Проверено.")
            window.applied_rewrite_original_sql = original
            window.applied_rewrite_sql = rewritten
            window.rollback_rewrite_button.setEnabled(True)

            window._set_query_actions_enabled(False)

            self.assertFalse(window.analyze_button.isEnabled())
            self.assertFalse(window.deep_analyze_button.isEnabled())
            self.assertFalse(window.rewrite_analyze_button.isEnabled())
            self.assertFalse(window.apply_rewrite_button.isEnabled())
            self.assertFalse(window.rollback_rewrite_button.isEnabled())

            window._set_query_actions_enabled(True)
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertTrue(window.rollback_rewrite_button.isEnabled())
        finally:
            window.close()

    def test_cancel_deep_analysis_keeps_controls_locked_until_worker_finishes(self):
        from pqo.analysis_control import AnalysisControl

        window = MainWindow()
        try:
            window._deep_control = AnalysisControl()
            control = window._deep_control
            window._set_query_actions_enabled(False)
            window._cancel_deep_analysis()
            self.assertTrue(control.cancelled.is_set())
            self.assertFalse(window.analyze_button.isEnabled())
            window._deep_analysis_failed("AnalysisCancelled: cancelled")
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertIsNone(window._deep_control)
            self.assertIn("откачены", window.recommendation_text.toPlainText())
        finally:
            window.close()

    def test_primary_analysis_uses_prediction_without_execution(self):
        window = MainWindow()
        try:
            window._start_worker = lambda _worker: None
            with patch("pqo.desktop.Worker") as worker_class:
                window._start_analysis()

            self.assertIs(worker_class.call_args.args[0], analyze_query)
            self.assertNotIn("repetitions", worker_class.call_args.kwargs)
            self.assertTrue(window._query_operation_active)
            self.assertFalse(window.analyze_button.isEnabled())
        finally:
            window.close()

    def test_sql_editor_identity_ignores_formatting_and_final_semicolon(self):
        compact = "SELECT * FROM aviation.flights;"
        formatted = "SELECT\n  *\nFROM aviation.flights"

        self.assertEqual(
            _sql_editor_identity(compact), _sql_editor_identity(formatted)
        )

    def test_rewrite_diff_makes_removed_order_by_explicit(self):
        diff = _format_sql_rewrite_diff(
            "SELECT * FROM (SELECT id FROM t ORDER BY id) q",
            "SELECT * FROM (SELECT id FROM t) q",
        )

        self.assertIn("--- исходный SQL", diff)
        self.assertIn("+++ переписанный SQL", diff)
        self.assertIn("-  ORDER BY", diff)

    def test_rewrite_result_does_not_append_structural_hypotheses(self):
        window = MainWindow()
        try:
            original = "SELECT * FROM (SELECT id FROM aviation.flights ORDER BY id) q"
            rewritten = "SELECT * FROM (SELECT id FROM aviation.flights) q"
            window.sql_editor.setPlainText(original)
            candidate = SQLRewriteCandidate(
                "subquery-order-without-limit",
                "Удалить ORDER BY",
                rewritten,
            )
            evaluation = SQLRewriteEvaluation(
                candidate,
                True,
                100.0,
                50.0,
                0.5,
                True,
                None,
            )
            plan = SQLRewritePlan(original, (evaluation,), evaluation, 0, 0, 0, "recommended")

            window._show_rewrite_analysis(plan)

            text = window.recommendation_text.toPlainText()
            self.assertIn("ТОЧНОЕ ИЗМЕНЕНИЕ", text)
            self.assertNotIn("СТРУКТУРНЫЕ РЕКОМЕНДАЦИИ", text)
            self.assertTrue(window.structural_management_group.isHidden())
            self.assertTrue(window.apply_rewrite_button.isEnabled())
        finally:
            window.close()

    def test_no_rewrite_candidates_says_database_was_not_executed(self):
        window = MainWindow()
        try:
            plan = SQLRewritePlan(
                "SELECT * FROM retail.customer_orders",
                (),
                None,
                50.0,
                5.0,
                0.05,
                "no_candidates",
            )

            window._show_rewrite_analysis(plan)

            text = window.recommendation_text.toPlainText()
            self.assertIn("ФАКТИЧЕСКАЯ ПРОВЕРКА НЕ ЗАПУСКАЛАСЬ", text)
            self.assertIn("PostgreSQL не выполнялся", text)
            self.assertIn("Проверены 11 правил", text)
            self.assertIn("вариантов нет", window.rewrite_notice.text())
            self.assertFalse(window.rewrite_notice.isHidden())
            window.sql_editor.setPlainText("SELECT 1")
            self.assertTrue(window.rewrite_notice.isHidden())
            self.assertIn("Объединить коррелированные агрегаты", text)
            self.assertIn("ввести справа", text)
            self.assertNotIn("Обнаруженная предварительная агрегация", text)
            self.assertNotIn("ускоряющий вариант не найден", text)
        finally:
            window.close()

    def test_shared_aggregate_rewrite_populates_right_editor_without_changing_left(self):
        from pqo.sql_rewrite import generate_sql_rewrites
        original = (Path(__file__).resolve().parents[1] /
                    "examples/aviation-shared-aggregates-rewrite.sql").read_text(encoding="utf-8")
        candidate = next(item for item in generate_sql_rewrites(original)
                         if item.rule_id == "shared-scalar-aggregates-to-grouped-join")
        evaluation = SQLRewriteEvaluation(candidate, True, 1690.348, 222.292, .868, True, None)
        plan = SQLRewritePlan(original, (evaluation,), evaluation, 50, 5, .05, "recommended")
        window = MainWindow()
        try:
            window.sql_editor.setPlainText(original)
            window._show_rewrite_analysis(plan)
            self.assertEqual(window.sql_editor.toPlainText(), original)
            self.assertEqual(window.rewrite_editor.toPlainText(), candidate.sql_text)
            self.assertTrue(window.apply_rewrite_button.isEnabled())
            self.assertNotIn("СТРУКТУРНЫЕ РЕКОМЕНДАЦИИ", window.recommendation_text.toPlainText())
        finally:
            window.close()

    def test_default_models_are_part_of_repository(self):
        self.assertTrue(DEFAULT_XGB_MODEL.is_file())
        self.assertTrue(DEFAULT_DQN_MODEL.is_file())
        self.assertTrue(DEFAULT_SEQUENTIAL_DQN_MODEL.is_file())
        self.assertTrue(DEFAULT_STRATEGY_MODEL.is_file())
        self.assertTrue(DEFAULT_DQN_STRESS_METRICS.is_file())

    def test_sequential_index_ddl_is_readable(self):
        action = IndexAction.create(
            "public", "orders", ("customer_id",), ("created_at",)
        )

        self.assertEqual(
            _format_index_action(action),
            'CREATE INDEX ON "public"."orders" ("customer_id") '
            'INCLUDE ("created_at");',
        )

    def test_prediction_is_clearly_labeled_as_estimate_with_range(self):
        prediction = QueryTimePrediction(
            sql_text="SELECT 1",
            predicted_time_ms=11.4,
            estimated_total_cost=1.0,
            estimated_plan_rows=1.0,
            root_node_type="Result",
            plan_node_count=1,
            uncalibrated_time_ms=11.4,
            calibration_factor=1.0,
            calibration_sample_count=0,
            error_mae_ms=14.18,
            error_source="контрольная MAE модели",
        )

        text = _format_prediction(prediction)

        self.assertIn("прогноз: ML-модель", text)
        self.assertNotIn("ориентир", text)

    def test_prediction_labels_automatic_database_profile(self):
        prediction = QueryTimePrediction(
            sql_text="SELECT 1",
            predicted_time_ms=4.1,
            estimated_total_cost=1.0,
            estimated_plan_rows=1.0,
            root_node_type="Result",
            plan_node_count=1,
            uncalibrated_time_ms=11.4,
            calibration_factor=4.1 / 11.4,
            calibration_sample_count=1,
            estimate_source="database_profile",
        )

        text = _format_prediction(prediction)

        self.assertIn("прогноз: ML + профиль БД", text)

    def test_previous_measurement_is_not_labelled_as_model_prediction(self):
        prediction = QueryTimePrediction(
            sql_text="SELECT 1", predicted_time_ms=777.77,
            estimated_total_cost=1, estimated_plan_rows=1,
            root_node_type="Result", plan_node_count=1,
            uncalibrated_time_ms=1779.01, calibration_factor=0.437,
            calibration_sample_count=1, estimate_source="exact_history",
        )
        text = _format_prediction(prediction)
        self.assertIn("измерениям этого SQL", text)
        self.assertNotIn("прогноз:", text)
        self.assertNotIn("до калибровки", text)

    def test_index_impact_discloses_write_and_storage_tradeoffs(self):
        step = SequentialRecommendationStep(
            action=IndexAction.create(
                "aviation", "tickets", ("book_ref",), ("ticket_no",)
            ),
            predicted_q=0.8,
            measured_reward=0.4,
            before_time_ms=150.0,
            after_time_ms=50.0,
            index_size_bytes=5 * 1024 * 1024,
            creation_time_ms=42.0,
            used_by_postgresql=True,
            target_relation_size_bytes=100 * 1024 * 1024,
            target_existing_index_count=1,
            target_insert_count=100,
            target_update_count=20,
            target_delete_count=5,
        )

        text = _format_index_impact(step)

        self.assertIn("5.0%", text)
        self.assertIn("после применения будет 2", text)
        self.assertIn("INSERT=100", text)
        self.assertIn("UPDATE индексируемых", text)
        self.assertIn("CREATE INDEX CONCURRENTLY", text)
        self.assertIn("только для текущего SQL", text)
        self.assertIn("влияние на задержку записи — не определено", text)
        self.assertIn("не ключи поиска или сортировки", text)
        self.assertIn("HOT", text)
        self.assertIn("без стоимости записи", text)
        self.assertIn("сведения о существующих ведущих ключах не сохранены", text)
        from dataclasses import replace
        overlap = _format_index_impact(replace(step, target_key_overlap_count=1, creation_time_ms=1200.0))
        self.assertIn("1 существующих индексов", overlap)
        self.assertIn("старые индексы автоматически не удаляются", overlap)
        self.assertIn("около 12 повторений", overlap)

    def test_deep_measurement_plan_reports_median_range(self):
        from pqo.sequential_recommendation import SequentialRecommendationPlan

        plan = SequentialRecommendationPlan(
            steps=(),
            baseline_time_ms=6.0,
            final_time_ms=6.0,
            storage_budget_bytes=1024,
            used_budget_bytes=0,
            candidate_count=1,
            decision_threshold=0.1,
            minimum_baseline_time_ms=5.0,
            minimum_absolute_improvement_ms=1.0,
            terminal_reason="model_stop",
            baseline_samples_ms=(8.0, 4.0, 6.0, 5.0, 7.0),
        )

        self.assertEqual(plan.baseline_min_time_ms, 4.0)
        self.assertEqual(plan.baseline_max_time_ms, 8.0)

    def test_deep_timeout_is_shown_as_lower_bound_without_error_dialog(self):
        from pqo.sequential_recommendation import SequentialRecommendationPlan

        window = MainWindow()
        try:
            plan = SequentialRecommendationPlan(
                steps=(),
                baseline_time_ms=30_000.0,
                final_time_ms=30_000.0,
                storage_budget_bytes=1024,
                used_budget_bytes=0,
                candidate_count=1,
                decision_threshold=0.1,
                minimum_baseline_time_ms=5.0,
                minimum_absolute_improvement_ms=1.0,
                terminal_reason="statement_timeout",
                baseline_timed_out=True,
            )

            window._show_sequential_analysis(plan)

            self.assertIn("более 30000 мс", window.predicted_value.text())
            self.assertIn("Транзакция восстановлена", window.recommendation_text.toPlainText())
            self.assertTrue(window.deep_analyze_button.isEnabled())
        finally:
            window.close()

    def test_structural_recommendation_is_readable(self):
        text = _format_structural_recommendations(
            (
                StructuralRecommendation(
                    RecommendationCategory.SORT,
                    "large-offset-pagination",
                    RecommendationPriority.HIGH,
                    "Использовать keyset",
                    "OFFSET 5000",
                    "Передать последний ключ",
                    "Сравнить EXPLAIN ANALYZE",
                ),
            )
        )

        self.assertIn("Сортировка", text)
        self.assertIn("требуют отдельной проверки", text)
        self.assertIn("приоритет: высокий", text)
        self.assertIn("OFFSET 5000", text)

    def test_fast_analysis_does_not_present_weak_index_as_recommendation(self):
        window = MainWindow()
        try:
            prediction = QueryTimePrediction(
                sql_text="SELECT * FROM retail.customer_orders",
                predicted_time_ms=1000.0,
                estimated_total_cost=100.0,
                estimated_plan_rows=1000.0,
                root_node_type="Seq Scan",
                plan_node_count=1,
                uncalibrated_time_ms=1000.0,
                calibration_factor=1.0,
                calibration_sample_count=0,
            )
            structural = StructuralRecommendation(
                RecommendationCategory.AGGREGATION,
                "aggregate-after-multiple-one-to-many-joins",
                RecommendationPriority.HIGH,
                "Устранить размножение строк до итоговой агрегации",
                "Несколько дочерних потоков.",
                "Предварительно агрегировать.",
                "Сравнить результаты.",
            )
            candidate = IndexRecommendation(
                IndexAction.create(
                    "retail", "customer_orders", ("shipping_address_id",)
                ),
                0.03,
                21,
            )
            analysis = QueryAnalysis(
                prediction,
                candidate,
                50.0,
                None,
                strategy_prediction={
                    "strategy": "NOOP",
                    "probabilities": {
                        "NOOP": 0.678,
                        "CREATE_INDEX": 0.258,
                        "REWRITE_QUERY": 0.064,
                    },
                },
                structural_recommendations=(structural,),
            )

            window._show_analysis(analysis)

            text = window.recommendation_text.toPlainText()
            self.assertIn(
                "ОБЫЧНЫЙ ИНДЕКС ПРЕДВАРИТЕЛЬНО НЕ РЕКОМЕНДУЕТСЯ", text
            )
            self.assertIn("ПРИОРИТЕТНОЕ НАПРАВЛЕНИЕ: структурная проверка", text)
            self.assertIn("СЛАБЫЙ ИНДЕКСНЫЙ КАНДИДАТ — только справочно", text)
            self.assertNotIn("\nИНДЕКСНЫЙ КАНДИДАТ:\n", text)
        finally:
            window.close()

    def test_persisted_structural_recommendation_enables_feedback(self):
        window = MainWindow()
        try:
            recommendation = StructuralRecommendation(
                RecommendationCategory.AGGREGATION,
                "non-aggregate-having-filter",
                RecommendationPriority.HIGH,
                "Проверить JOIN",
                "Нет ON",
                "Добавить условие",
                "Сравнить EXPLAIN",
                recommendation_id=42,
            )

            window._configure_structural_feedback((recommendation,))

            self.assertEqual(window.structural_recommendation_combo.currentData(), 42)
            self.assertTrue(window.accept_structural_button.isEnabled())
            self.assertFalse(window.save_structural_measurement_button.isEnabled())
            window.structural_feedback_state[42] = "accepted"
            window._sync_structural_feedback_controls()
            self.assertTrue(window.structural_before_spin.isEnabled())
            self.assertTrue(window.structural_after_spin.isEnabled())
            self.assertTrue(window.save_structural_measurement_button.isEnabled())
            self.assertTrue(window.validate_structural_button.isEnabled())
        finally:
            window.close()

    def test_history_displays_measured_sequential_result(self):
        window = MainWindow()
        try:
            record = HistoryRecord(
                query_run_id=4,
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                status="completed",
                sql_text="SELECT 1",
                predicted_time_ms=11.4,
                root_node_type="Result",
                recommended_table=None,
                recommended_columns=(),
                predicted_reward=None,
                sequential_analysis_id=8,
                measured_baseline_time_ms=4.12,
                measured_final_time_ms=3.09,
                measured_improvement_ratio=0.25,
                sequential_terminal_reason="max_steps",
                sequential_steps=(
                    HistorySequentialStep(
                        1,
                        'CREATE INDEX ON "public"."orders" ("customer_id");',
                        0.75,
                        0.23,
                        4.12,
                        3.09,
                        65536,
                        True,
                    ),
                ),
                structural_feedback=(
                    HistoryStructuralFeedback(
                        15,
                        "sort",
                        "large-offset-pagination",
                        "Использовать keyset",
                        "accepted",
                        0.35,
                        "improved",
                    ),
                ),
            )

            window._show_history([record])

            self.assertEqual(window.history_table.columnCount(), 12)
            window.history_table.setCurrentCell(0, 0)
            self.assertIn("не сохранён", window.history_candidate_details.toPlainText())
            self.assertTrue(window.history_candidate_details.isReadOnly())
            self.assertEqual(window.history_table.item(0, 0).text(), "4/8")
            self.assertIn("4.12", window.history_table.item(0, 7).text())
            self.assertEqual(window.history_table.item(0, 8).text(), "+25.0%")
            self.assertIn("улучшение", window.history_table.item(0, 10).text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
