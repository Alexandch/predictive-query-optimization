import os
from pathlib import Path
import unittest
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
    _format_index_action,
    _format_prediction,
    _format_structural_recommendations,
)
from pqo.index_actions import IndexAction
from pqo.prediction import QueryTimePrediction
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


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_contains_complete_workflow(self):
        window = MainWindow()
        try:
            self.assertEqual(window.tabs.count(), 5)
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(window.tabs.count())],
                ["Анализ", "История", "Эксперименты", "Настройки", "Обучение"],
            )
            self.assertTrue(window.analyze_button.isEnabled())
            self.assertEqual(
                window.analyze_button.text(), "Анализировать и измерить"
            )
            self.assertIn("SELECT", window.sql_editor.toPlainText())
            self.assertIsNotNone(window.persist_check)
            self.assertTrue(window.calibrate_button.isHidden())
            self.assertTrue(window.batch_calibrate_button.isEnabled())
            self.assertTrue(window.deep_analyze_button.isEnabled())
            self.assertTrue(window.rewrite_analyze_button.isEnabled())
            self.assertTrue(window.analysis_scroll.widgetResizable())
            self.assertFalse(window.apply_indexes_button.isEnabled())
            self.assertFalse(window.rollback_indexes_button.isEnabled())
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

        self.assertIn("среднее время по ML-модели", text)
        self.assertIn("0.00–25.58 мс", text)

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
        )

        text = _format_prediction(prediction)

        self.assertIn("адаптивная оценка ML + профиль БД", text)
        self.assertIn("до калибровки 11.40 мс", text)

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
        self.assertIn("приоритет: высокий", text)
        self.assertIn("OFFSET 5000", text)

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

            self.assertEqual(window.history_table.columnCount(), 11)
            self.assertEqual(window.history_table.item(0, 0).text(), "4/8")
            self.assertIn("4.12", window.history_table.item(0, 7).text())
            self.assertEqual(window.history_table.item(0, 8).text(), "+25.0%")
            self.assertIn("улучшение", window.history_table.item(0, 10).text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
