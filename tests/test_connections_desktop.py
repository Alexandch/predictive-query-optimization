import os
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication
from pqo.connections import (
    ConnectionProfile,
    PROFILE_KEY,
    SERVICE_KEY,
    decode_profiles,
)
from pqo.desktop import MainWindow


class DesktopConnectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.preferences = QSettings(
            str(Path(temporary.name) / "prefs.ini"), QSettings.Format.IniFormat
        )
        patcher = patch("pqo.desktop.QSettings", return_value=self.preferences)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def test_separate_endpoints_no_automatic_connections(self):
        window = self.window
        source = window._database_settings()
        service = window._service_database_settings()
        self.assertEqual(service.dbname, "pqo_service")
        self.assertNotEqual(source.dbname, service.dbname)
        with patch("psycopg.connect") as connect:
            window.service_database_edit.setText("metadata")
            window.database_edit.setText("other-source")
        connect.assert_not_called()
        self.assertEqual(window._service_database_settings().dbname, "metadata")
        self.assertEqual(window._database_settings().dbname, "other-source")
        window.service_database_edit.setText("other-source")
        with self.assertRaisesRegex(ValueError, "должны быть разными"):
            window._service_database_settings()

    def test_save_and_switch_profiles_never_persists_password_or_model_changes(self):
        window = self.window
        original = window._database_settings()
        profile = ConnectionProfile.from_settings(
            "Other",
            replace(original, dbname="other", host="remote.invalid", sslmode="require"),
        )
        window._connection_profiles.append(profile)
        window.password_edit.setText("session-secret")
        window._fill_profile_combos()
        model = window.xgb_model_edit.text()
        window.active_database_combo.setCurrentIndex(1)
        self.assertEqual(window.database_edit.text(), "other")
        self.assertEqual(window.password_edit.text(), "")
        self.assertIsNone(window.last_sequential_plan)
        window.password_edit.setText("remote-secret")
        window.service_password_edit.setText("service-secret")
        window._save_settings()
        persisted = str(self.preferences.value(PROFILE_KEY)) + str(
            self.preferences.value(SERVICE_KEY)
        )
        for secret in ("session-secret", "remote-secret", "service-secret"):
            self.assertNotIn(secret, persisted)
        window.active_database_combo.setCurrentIndex(0)
        self.assertEqual(window.password_edit.text(), "session-secret")
        self.assertEqual(window.xgb_model_edit.text(), model)
        self.assertEqual(window.service_password_edit.text(), "service-secret")
        self.assertEqual(len(decode_profiles(self.preferences.value(PROFILE_KEY))), 2)

    def test_schema_checkboxes_multiple_and_stale_result_discarded(self):
        window = self.window
        settings = window._source_catalog_settings()
        result = (settings.dbname, (("public", 1), ("sales", 2), ("crm", 3)))
        window._source_schemas_loaded(settings, result)
        window._set_all_schemas(True)
        self.assertEqual(
            window._database_settings().allowed_schemas,
            frozenset({"public", "sales", "crm"}),
        )
        window._set_all_schemas(False)
        with patch.object(window, "_start_worker") as start:
            window._load_source_schemas()
        self.assertEqual(
            start.call_count, 1
        )  # Empty selection must not prevent discovery.
        window.database_edit.setText("another")
        window._source_schemas_loaded(settings, result)
        self.assertEqual(window.source_schema_list.count(), 0)
        window.allowed_schemas_edit.setText("public")

    def test_source_switch_invalidates_verified_results_but_not_service_scope(self):
        window = self.window
        service = window._service_database_settings()
        window.last_query_run_id = 52
        window.verified_rewrite_sql = "SELECT 2"
        window.rewrite_editor.setPlainText("SELECT 2")
        window.database_edit.setText("another")
        self.assertIsNone(window.last_query_run_id)
        self.assertIsNone(window.verified_rewrite_sql)
        self.assertFalse(window.apply_rewrite_button.isEnabled())
        self.assertEqual(window.rewrite_editor.toPlainText(), "")
        self.assertTrue(window._history_settings_match(service))

    def test_backend_receives_service_connection_only_for_history(self):
        window = self.window
        window.persist_check.setChecked(True)
        window.sql_editor.setPlainText("SELECT 1")
        with patch.object(window, "_start_worker") as start:
            window._start_analysis()
        worker = start.call_args.args[0]
        self.assertEqual(worker.args[3], window._database_settings())
        self.assertEqual(
            worker.kwargs["history_settings"], window._service_database_settings()
        )
        self.assertNotEqual(
            worker.args[3].dbname, worker.kwargs["history_settings"].dbname
        )
        window._set_query_actions_enabled(True)
