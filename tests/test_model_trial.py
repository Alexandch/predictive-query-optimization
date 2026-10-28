import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication
from pqo.model_trial import activate_trial, rollback_trial, trial_state


class ModelTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "original.joblib"
        self.old.write_bytes(b"old verified weights")
        self.new = self.root / "trial.joblib"
        self.new.write_bytes(b"new weights")
        self.preferences = QSettings(str(self.root / "test.ini"), QSettings.Format.IniFormat)
        validation = patch("pqo.model_trial.validate_trial", side_effect=lambda path: Path(path).resolve())
        validation.start()
        self.addCleanup(validation.stop)

    def activate(self):
        return activate_trial(self.preferences, self.new, self.old, self.root / "backups")

    def test_backup_rollback_and_idempotent_activation(self):
        self.activate()
        state = trial_state(self.preferences)
        self.assertEqual(Path(state["backup_path"]).read_bytes(), self.old.read_bytes())
        activate_trial(self.preferences, self.new, self.new, self.root / "backups")
        self.assertEqual(trial_state(self.preferences), state)
        self.assertEqual(rollback_trial(self.preferences, self.new), self.old)
        self.assertIsNone(trial_state(self.preferences))

    def test_changed_original_uses_backup_without_overwriting(self):
        self.activate()
        self.old.write_bytes(b"independently changed")
        restored = rollback_trial(self.preferences, self.new)
        self.assertEqual(restored.read_bytes(), b"old verified weights")
        self.assertEqual(self.old.read_bytes(), b"independently changed")

    def test_corrupt_backup_refuses_rollback(self):
        self.activate()
        Path(trial_state(self.preferences)["backup_path"]).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            rollback_trial(self.preferences, self.new)
        self.assertEqual(self.preferences.value("xgb_model"), str(self.new))

    def test_desktop_switch_is_blocked_during_work(self):
        from pqo.desktop import MainWindow
        app = QApplication.instance() or QApplication([])
        with patch("pqo.desktop.QSettings", return_value=self.preferences):
            window = MainWindow()
        try:
            window._query_operation_active = True
            with patch("pqo.desktop.activate_trial") as activate:
                window._activate_model_trial()
                activate.assert_not_called()
            window._query_operation_active = False
            window._active_workers.add(object())
            with patch("pqo.desktop.rollback_trial") as rollback:
                window._rollback_model_trial()
                rollback.assert_not_called()
            window._active_workers.clear()
        finally:
            window.close()

    def test_desktop_switch_clears_forecast_and_restores_selection(self):
        from pqo.desktop import MainWindow
        app = QApplication.instance() or QApplication([])
        self.preferences.setValue("xgb_model", str(self.old))
        with patch("pqo.desktop.QSettings", return_value=self.preferences):
            window = MainWindow()
        try:
            with patch("pqo.desktop.materialize_trial", return_value=self.new):
                window._activate_model_trial()
            self.assertEqual(Path(window.xgb_model_edit.text()), self.new)
            self.assertEqual(window.predicted_value.text(), "—")
            self.assertTrue(window.restore_model_button.isEnabled())
            window._rollback_model_trial()
            self.assertEqual(Path(window.xgb_model_edit.text()), self.old)
        finally:
            window.close()
