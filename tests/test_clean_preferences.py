import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from pqo.desktop_preferences import open_preferences


class CleanPreferencesTests(TestCase):
    def test_no_override_retains_the_native_location(self):
        constructor = Mock()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(open_preferences(constructor), constructor.return_value)
        constructor.assert_called_once_with("PQO", "PredictiveQueryOptimization")

    def test_file_override_is_empty_then_persists_without_native_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "isolated" / "preferences.ini"
            with patch.dict(os.environ, {"PQO_SETTINGS_FILE": str(filename)}):
                preferences = open_preferences(QSettings)
                self.assertEqual(preferences.allKeys(), [])
                self.assertFalse(preferences.fallbacksEnabled())
                preferences.setValue("db_name", "only-in-file")
                preferences.sync()
                reopened = open_preferences(QSettings)
                self.assertEqual(reopened.value("db_name"), "only-in-file")
                self.assertEqual(Path(reopened.fileName()), filename)

    def test_invalid_override_never_opens_native_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("", "  ", "relative.ini", directory):
                constructor = Mock()
                with patch.dict(os.environ, {"PQO_SETTINGS_FILE": filename}):
                    with self.assertRaises(ValueError):
                        open_preferences(constructor)
                constructor.assert_not_called()

    def test_new_gui_uses_plan_v1_but_saved_selection_is_preserved(self):
        from pqo.desktop import MainWindow, DEFAULT_XGB_MODEL

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "preferences.ini"
            with patch.dict(os.environ, {"PQO_SETTINGS_FILE": str(filename)}):
                first = MainWindow()
                try:
                    self.assertEqual(
                        Path(first.xgb_model_edit.text()), DEFAULT_XGB_MODEL
                    )
                    self.assertIn("trial", DEFAULT_XGB_MODEL.parts)
                    self.assertTrue(DEFAULT_XGB_MODEL.is_file())
                    self.assertFalse(first.activate_trial_button.isEnabled())
                    self.assertFalse(first.restore_model_button.isEnabled())
                    self.assertIn("комплекта поставки", first.trial_model_status.text())
                    self.assertEqual(first.preferences.allKeys(), [])
                    first.preferences.setValue("xgb_model", "user-selected.joblib")
                    first.preferences.sync()
                finally:
                    first.close()
                reopened = MainWindow()
                try:
                    self.assertEqual(
                        reopened.xgb_model_edit.text(), "user-selected.joblib"
                    )
                finally:
                    reopened.close()
                app.processEvents()


class CleanSmokeGuardTests(TestCase):
    def test_no_opt_in_stops_before_config_or_connection(self):
        from pqo.clean_profile_smoke import _read_config

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("psycopg.connect") as connect,
        ):
            with self.assertRaisesRegex(ValueError, "opt-in"):
                _read_config()
        connect.assert_not_called()

    def test_unknown_phase_is_not_treated_as_a_fresh_profile(self):
        from pqo.clean_profile_smoke import _read_config

        with patch.dict(
            os.environ,
            {"PQO_CLEAN_ACCEPTANCE_CONFIRMED": "1", "PQO_CLEAN_PHASE": "wrong"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "phase"):
                _read_config()
