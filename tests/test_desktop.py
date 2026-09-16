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
    _format_structural_recommendations,
)
from pqo.index_actions import IndexAction
from pqo.history import HistoryRecord, HistorySequentialStep
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
            self.assertIn("SELECT", window.sql_editor.toPlainText())
            self.assertIsNotNone(window.persist_check)
            self.assertIsNotNone(window.calibrate_button)
            self.assertTrue(window.deep_analyze_button.isEnabled())
            self.assertTrue(window.rewrite_analyze_button.isEnabled())
            self.assertEqual(
                Path(window.strategy_model_edit.text()), DEFAULT_STRATEGY_MODEL
            )
            self.assertFalse(window.use_calibration_check.isChecked())
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
            )

            window._show_history([record])

            self.assertEqual(window.history_table.columnCount(), 10)
            self.assertEqual(window.history_table.item(0, 0).text(), "4/8")
            self.assertIn("4.12", window.history_table.item(0, 7).text())
            self.assertEqual(window.history_table.item(0, 8).text(), "+25.0%")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
