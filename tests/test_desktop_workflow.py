import json
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from pqo.desktop import MainWindow
from pqo.connections import endpoint, PROFILE_KEY
from pqo.laboratory import Laboratory
from pqo.transaction_inspection import inspect_transaction

TEXT = """BEGIN;UPDATE university.items SET value=3 WHERE id=1;
SELECT id,value FROM university.items WHERE id=1 OR id=2 ORDER BY id;COMMIT;"""


class DesktopWorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prefs = QSettings(
            str(Path(directory.name) / "settings.ini"), QSettings.Format.IniFormat
        )
        prefs.setValue("db_name", "source")
        prefs.setValue("allowed_schemas", "university")
        prefs.setValue("xgb_model", "preserved-model.joblib")
        patcher = patch("pqo.desktop.QSettings", return_value=prefs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def test_home_navigation_does_not_change_sql_models_or_make_connections(self):
        w = self.window
        before = w.sql_editor.toPlainText()
        self.assertIs(w.workflow_stack.currentWidget(), w.workflow_home)
        self.assertFalse(w.workflow_copy_button.isEnabled())
        with patch("psycopg.connect") as connect:
            w._workflow_open_settings()
            self.assertIs(w.workflow_stack.currentWidget(), w.tabs)
            w._workflow_open_transaction()
            w._workflow_show_home()
            w._workflow_open_tab(0)
        connect.assert_not_called()
        self.assertEqual(w.sql_editor.toPlainText(), before)
        self.assertEqual(w.xgb_model_edit.text(), "preserved-model.joblib")

    def test_schema_discovery_selects_available_schemas_for_first_connection(self):
        w = self.window
        s = w._source_catalog_settings()
        w._source_schemas_loaded(s, (s.dbname, (("sales", 2), ("crm", 1))))
        self.assertEqual(
            w._database_settings().allowed_schemas, frozenset({"sales", "crm"})
        )
        self.assertTrue(w.workflow_copy_button.isEnabled())
        self.assertFalse(w.workflow_query_button.isEnabled())
        self.assertIn("Следующий шаг", w.workflow_status.text())

    def test_copy_registration_preserves_source_service_model_and_uses_baseline(self):
        w = self.window
        source = w._database_settings()
        service = w._service_database_settings()
        profile = w._active_profile_id
        result = Laboratory(
            source.dbname,
            "pqo_new_lab",
            "pqo_new_lab_base",
            (("pqo_new_lab_base", 1, 2), ("pqo_new_lab", 3, 2)),
            ("university",),
        )
        with patch("psycopg.connect") as connect:
            w._workflow_copy_ready((profile, source, result))
        connect.assert_not_called()
        self.assertEqual(w._database_settings().dbname, "pqo_new_lab")
        self.assertEqual(w._service_database_settings(), service)
        self.assertEqual(w.xgb_model_edit.text(), "preserved-model.joblib")
        self.assertTrue(
            any(
                p.id == profile and p.database == source.dbname
                for p in w._connection_profiles
            )
        )
        self.assertEqual(w.transaction_replay_reference.text(), "pqo_new_lab_base")
        self.assertEqual(
            w.transaction_select_rewrite_reference.text(), "pqo_new_lab_base"
        )
        self.assertFalse(w.transaction_replay_confirm.isChecked())
        self.assertTrue(w.workflow_query_button.isEnabled())
        saved = w.preferences.value(PROFILE_KEY)
        self.assertNotIn("password", saved)
        self.assertNotIn("PGPASSWORD", saved)

    def test_copy_on_another_server_registers_destination_credentials_only_in_memory(
        self,
    ):
        w = self.window
        source = w._database_settings()
        source_profile = w._active_profile_id
        destination = replace(
            source,
            host="test-server",
            port=5433,
            user="lab_owner",
            password="only-in-memory",
            sslmode="require",
        )
        result = Laboratory(
            source.dbname,
            "separate_lab",
            "separate_lab_base",
            (("separate_lab_base", 1, 2), ("separate_lab", 3, 2)),
            ("university",),
        )
        with patch("psycopg.connect") as connect:
            w._workflow_copy_ready((source_profile, source, result, destination))
        connect.assert_not_called()
        self.assertEqual(
            w._database_settings(), replace(destination, dbname="separate_lab")
        )
        self.assertNotIn(destination.password, str(w.preferences.value(PROFILE_KEY)))
        self.assertNotIn(
            destination.password, str(w.preferences.value("laboratories_v1"))
        )
        w._workflow_use_source()
        self.assertEqual(w._database_settings(), source)
        w._workflow_use_copy()
        self.assertEqual(
            w._database_settings(), replace(destination, dbname="separate_lab")
        )

    def test_selected_select_round_trip_preserves_all_other_commands(self):
        w = self.window
        w.transaction_editor.setPlainText(TEXT)
        w._transaction_inspected((TEXT, inspect_transaction(TEXT)))
        w.transaction_table.setCurrentCell(1, 0)
        w._workflow_send_select()
        self.assertIn("SELECT", w.sql_editor.toPlainText())
        self.assertNotIn("UPDATE", w.sql_editor.toPlainText())
        self.assertTrue(w.workflow_return_select_button.isEnabled())
        w.rewrite_editor.setPlainText(
            "SELECT id,value FROM university.items WHERE id IN (1,2) ORDER BY id"
        )
        with patch("psycopg.connect") as connect:
            w._workflow_return_select()
        connect.assert_not_called()
        self.assertEqual(w.transaction_editor.toPlainText(), TEXT)
        self.assertEqual(w.transaction_views.currentIndex(), 9)
        self.assertIn("IN", w.transaction_select_rewrite_variant.toPlainText())
        self.assertEqual(
            w._transaction_select_rewrite_options[
                w.transaction_select_rewrite_choice.currentIndex()
            ].number,
            2,
        )

    def test_stale_transaction_cannot_receive_old_select_variant(self):
        w = self.window
        w.transaction_editor.setPlainText(TEXT)
        w._transaction_inspected((TEXT, inspect_transaction(TEXT)))
        w.transaction_table.setCurrentCell(1, 0)
        w._workflow_send_select()
        w.rewrite_editor.setPlainText(
            "SELECT id,value FROM university.items WHERE id IN(1,2) ORDER BY id"
        )
        changed = TEXT.replace("value=3", "value=4")
        w.transaction_editor.setPlainText(changed)
        w._workflow_return_select()
        self.assertEqual(w.transaction_editor.toPlainText(), changed)
        self.assertIn("изменились", w.variant_status.text())
        self.assertFalse(w._transaction_select_rewrite_options)

    def test_basic_navigation_and_failure_leave_technical_tools_available_in_advanced_mode(
        self,
    ):
        w = self.window
        self.assertTrue(w.transaction_sections.item(3).isHidden())
        w.advanced_mode_check.setChecked(True)
        self.assertFalse(w.transaction_sections.item(3).isHidden())
        w._workflow_copy_failed("Копия не готова: нет прав.")
        self.assertIn("нет прав", w.workflow_status.text())
        self.assertFalse(w._workflow_labs)
        self.assertFalse(w.workflow_transaction_button.isEnabled())

    def test_corrupt_saved_stand_is_ignored(self):
        self.window.preferences.setValue(
            "laboratories_v1",
            json.dumps(
                [
                    {
                        "source_profile": "x",
                        "working_profile": "y",
                        "stand": "broken",
                        "endpoint": [],
                    }
                ]
            ),
        )
        reopened = MainWindow()
        self.addCleanup(reopened.close)
        self.assertFalse(reopened._workflow_labs)
        self.assertFalse(reopened.workflow_query_button.isEnabled())
