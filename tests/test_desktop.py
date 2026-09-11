import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pqo.desktop import (
    DEFAULT_DQN_MODEL,
    DEFAULT_DQN_STRESS_METRICS,
    DEFAULT_SEQUENTIAL_DQN_MODEL,
    DEFAULT_XGB_MODEL,
    MainWindow,
    _format_index_action,
)
from pqo.index_actions import IndexAction


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
            self.assertFalse(window.use_calibration_check.isChecked())
        finally:
            window.close()

    def test_default_models_are_part_of_repository(self):
        self.assertTrue(DEFAULT_XGB_MODEL.is_file())
        self.assertTrue(DEFAULT_DQN_MODEL.is_file())
        self.assertTrue(DEFAULT_SEQUENTIAL_DQN_MODEL.is_file())
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


if __name__ == "__main__":
    unittest.main()
