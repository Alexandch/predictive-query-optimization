import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from pqo.desktop import MainWindow, Worker
from pqo.desktop_preferences import read_number
from pqo.model_trial import TRIAL_STATE_KEY


class DesktopSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.preferences = QSettings(str(Path(temporary.name) / "preferences.ini"), QSettings.Format.IniFormat)
        patcher = patch("pqo.desktop.QSettings", return_value=self.preferences)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drain(self, window):
        deadline = time.monotonic() + 5
        while window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(window._active_workers)

    def test_basic_mode_keeps_core_workflow_and_does_not_switch_model(self):
        self.preferences.setValue("xgb_model", "chosen-model.joblib")
        window = MainWindow()
        try:
            visible = [window.tabs.tabText(i) for i in range(window.tabs.count()) if window.tabs.isTabVisible(i)]
            self.assertEqual(visible, ["Анализ", "Транзакции", "История", "Настройки"])
            with patch("psycopg.connect") as connect:
                window.advanced_mode_check.setChecked(True)
                self.assertTrue(window.tabs.isTabVisible(window.tabs.indexOf(window.training_page)))
                self.assertTrue(window.settings_form.isRowVisible(window._advanced_settings_rows[0]))
                window.tabs.setCurrentWidget(window.training_page)
                window.advanced_mode_check.setChecked(False)
                self.assertIs(window.tabs.currentWidget(), window.analysis_scroll)
            connect.assert_not_called()
            self.assertEqual(window.xgb_model_edit.text(), "chosen-model.joblib")
            self.assertFalse(window.settings_form.isRowVisible(window._advanced_settings_rows[0]))
        finally:
            window.close()

    def test_mode_persists_without_discarding_training_configuration(self):
        window = MainWindow()
        window.xgb_output_edit.setText("keep-my-output")
        window.advanced_mode_check.setChecked(True)
        window.advanced_mode_check.setChecked(False)
        self.assertEqual(window.xgb_output_edit.text(), "keep-my-output")
        window.advanced_mode_check.setChecked(True)
        window.close()
        reopened = MainWindow()
        try:
            self.assertTrue(reopened.advanced_mode_check.isChecked())
        finally:
            reopened.close()

    def test_all_workers_lock_connection_models_and_conflicting_actions(self):
        for fails in (False, True):
            window = MainWindow()
            release = Event()
            errors = []
            def operation():
                release.wait(3)
                if fails:
                    raise ValueError("expected failure")
            worker = Worker(operation)
            worker.signals.failed.connect(errors.append)
            try:
                window._start_worker(worker)
                for control in (window.analyze_button, window.host_edit, window.xgb_model_edit,
                                window.xgb_train_button, window.batch_calibrate_button, window.advanced_mode_check):
                    self.assertFalse(control.isEnabled())
                with patch.object(window, "_start_worker") as start, patch("pqo.desktop.QMessageBox.question") as confirm:
                    for method in (window._start_analysis, window._start_sequential_analysis,
                                   window._start_apply_indexes, window._start_rollback_indexes,
                                   window._start_variant_comparison, window._start_xgb_training,
                                   window._start_dqn_training, window._start_batch_calibration, window._test_connection):
                        method()
                start.assert_not_called()
                confirm.assert_not_called()
                release.set()
                self.drain(window)
                self.assertEqual(bool(errors), fails)
                self.assertTrue(window.analyze_button.isEnabled())
                self.assertTrue(window.host_edit.isEnabled())
                self.assertTrue(window.xgb_train_button.isEnabled())
            finally:
                release.set()
                self.drain(window)
                window.close()

    def test_invalid_settings_never_start_query_or_leave_controls_locked(self):
        for field in ("database_edit", "host_edit", "user_edit", "allowed_schemas_edit"):
            window = MainWindow()
            try:
                getattr(window, field).setText("")
                with patch.object(window, "_start_worker") as start, patch("pqo.desktop.QMessageBox.warning") as warning:
                    for method in (window._start_analysis, window._start_sequential_analysis,
                                   window._start_rewrite_analysis, window._test_connection, window._save_settings):
                        method()
                start.assert_not_called()
                self.assertTrue(warning.called)
                self.assertFalse(window._query_operation_active)
                self.assertTrue(window.analyze_button.isEnabled())
            finally:
                window.allowed_schemas_edit.setText("public")
                window.close()

    def test_corrupt_trial_state_does_not_crash_startup_or_erase_backup_record(self):
        for stored in ("{broken", "[]", '{"trial_path":"bad"}', '{"trial_path":null}'):
            self.preferences.setValue(TRIAL_STATE_KEY, stored)
            window = MainWindow()
            try:
                self.assertIn("повреждена", window.trial_model_status.text())
                self.assertFalse(window.restore_model_button.isEnabled())
                self.assertEqual(self.preferences.value(TRIAL_STATE_KEY), stored)
            finally:
                window.close()

    def test_bad_numeric_preferences_do_not_crash_or_get_rewritten(self):
        for key, value in (("db_port", "not-a-port"), ("threshold_ms", "nan"),
                           ("minimum_gain_ms", "infinity"), ("minimum_gain_percent", "garbage")):
            self.preferences.setValue(key, value)
        window = MainWindow()
        try:
            self.assertEqual(window.port_spin.value(), 55432)
            self.assertEqual(window.threshold_spin.value(), 50)
            self.assertEqual(window.minimum_gain_spin.value(), 5)
            self.assertEqual(window.minimum_gain_percent_spin.value(), 5)
            self.assertEqual(self.preferences.value("db_port"), "not-a-port")
        finally:
            window.close()
        for value in (None, True, -1, 100000, "NaN", "Infinity", "12.3"):
            self.preferences.setValue("number", value)
            self.assertEqual(read_number(self.preferences, "number", 55432,
                                         minimum=1, maximum=65535, integer=True), 55432)
