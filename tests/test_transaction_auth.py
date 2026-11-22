import os
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from unittest import TestCase, skipUnless
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox
import psycopg

from pqo.config import DatabaseSettings
from pqo.connections import PROFILE_KEY, password_failure
from pqo.desktop import MainWindow
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport


TEXT = (Path(__file__).resolve().parents[1] / 'examples/transactions/university-joined-writes.sql').read_text(encoding='utf-8')


class TransactionAuthTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.prefs = QSettings(str(Path(directory.name)/'prefs.ini'), QSettings.Format.IniFormat)
        self.prefs.setValue('db_name', 'lab')
        self.prefs.setValue('allowed_schemas', 'university')
        patcher = patch('pqo.desktop.QSettings', return_value=self.prefs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.password_edit.clear()
        self.window.transaction_editor.setPlainText(TEXT)
        self.window._transaction_inspected((TEXT, inspect_transaction(TEXT)))

    def drain(self):
        deadline = time.monotonic()+20
        while self.window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.window._active_workers)

    def missing(self, message='OperationalError: connection failed: fe_sendauth: no password supplied'):
        w = self.window
        w._transaction_planning_failed(message, TEXT, w._database_settings(), w._transaction_effective_sql())

    def test_classification_does_not_treat_network_or_ssl_failure_as_a_password_request(self):
        self.assertEqual(password_failure('fe_sendauth: no password supplied'), 'missing')
        self.assertEqual(password_failure('FATAL: password authentication failed for user "worker"'), 'invalid')
        for message in ('connection refused', 'could not translate host name', 'SSL error: certificate verify failed',
                'permission denied for relation enrollments', 'canceling statement due to statement timeout'):
            self.assertIsNone(password_failure(message))

    def test_auth_error_is_short_recoverable_and_keeps_sql_and_inspection(self):
        w = self.window
        self.missing()
        self.assertIn('Пароль не введён', w.transaction_status.text())
        self.assertNotIn('fe_sendauth', w.transaction_status.text())
        self.assertNotIn('Multiple connection attempts', w.transaction_status.text())
        self.assertFalse(w.transaction_password_button.isHidden())
        self.assertTrue(w.transaction_password_button.isEnabled())
        self.assertEqual(w.transaction_editor.toPlainText(), TEXT)
        self.assertEqual(w.transaction_inspection, inspect_transaction(TEXT))
        self.assertEqual(w.statusBar().currentMessage(), w.transaction_status.text())
        self.assertIsNone(w.transaction_plan_report)

    def test_cancel_empty_or_stale_dialog_does_not_change_credentials_or_run_anything(self):
        w = self.window
        self.missing()
        for answer in (('entered-but-cancelled', False), ('', True)):
            with patch('pqo.transaction_desktop.QInputDialog.getText', return_value=answer), \
                    patch.object(w, '_launch_transaction_planning') as launch:
                w.transaction_password_button.click()
            launch.assert_not_called()
            self.assertEqual(w.password_edit.text(), '')
        def switched(*args):
            w.database_edit.setText('other')
            return 'must-not-leak', True
        with patch('pqo.transaction_desktop.QInputDialog.getText', side_effect=switched), \
                patch.object(w, '_launch_transaction_planning') as launch:
            w.transaction_password_button.click()
        launch.assert_not_called()
        self.assertEqual(w.password_edit.text(), '')

    def test_sql_connection_and_parameter_changes_hide_obsolete_retry(self):
        w = self.window
        self.missing()
        w.transaction_editor.setPlainText(TEXT.replace("'completed'", "'active'"))
        self.assertTrue(w.transaction_password_button.isHidden())
        self.assertIsNone(w._transaction_auth_retry)
        w.transaction_editor.setPlainText(TEXT)
        w._transaction_inspected((TEXT, inspect_transaction(TEXT)))
        self.missing()
        w.user_edit.setText('another-role')
        self.assertTrue(w.transaction_password_button.isHidden())
        self.assertIsNone(w._transaction_auth_retry)

    def test_queued_failure_retry_stores_secret_only_in_session_and_does_not_reconfirm(self):
        w = self.window
        attempts = []
        def collect(text, settings, **kwargs):
            attempts.append(settings)
            if not settings.password:
                raise psycopg.OperationalError('connection failed: no password supplied')
            return TransactionPlanReport(inspect_transaction(text), settings.dbname,
                tuple(StatementPlan(s.number, 'control' if s.kind in {'savepoint','rollback_to','release'} else 'planned',
                    'test', None if s.kind in {'savepoint','rollback_to','release'} else [{'Plan': {'Node Type': 'Result'}}])
                    for s in inspect_transaction(text).statements))
        with patch('pqo.transaction_desktop.collect_transaction_plans', side_effect=collect), \
                patch('pqo.transaction_desktop.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as confirm:
            w.transaction_plan_button.click()
            self.drain()
            self.assertTrue(w.transaction_password_button.isEnabled())
            with patch('pqo.transaction_desktop.QInputDialog.getText', return_value=('session-retry-secret', True)) as dialog:
                w.transaction_password_button.click()
                self.assertFalse(w.analyze_button.isEnabled())
                self.assertFalse(w.transaction_password_button.isEnabled())
                self.drain()
            self.assertEqual(dialog.call_args.args[-1], QLineEdit.EchoMode.Password)
            confirm.assert_called_once()
        self.assertEqual([s.password for s in attempts], ['', 'session-retry-secret'])
        self.assertIsNotNone(w.transaction_plan_report)
        self.assertEqual(w.transaction_editor.toPlainText(), TEXT)
        self.assertTrue(w.transaction_password_button.isHidden())
        self.assertTrue(w.analyze_button.isEnabled())
        w._save_connection_profiles()
        self.prefs.sync()
        self.assertNotIn('session-retry-secret', str(self.prefs.value(PROFILE_KEY)))
        self.assertNotIn('session-retry-secret', Path(self.prefs.fileName()).read_text(encoding='utf-8'))

    @skipUnless(os.environ.get('PQO_UNIVERSITY_TESTS') == '1', 'Existing university lab, read-only EXPLAIN only')
    def test_real_missing_password_then_explain_joined_writes_without_executing_them(self):
        w = self.window
        settings = replace(DatabaseSettings.from_env(), dbname='pqo_university_index_lab', allowed_schemas=frozenset({'university'}))
        w.host_edit.setText(settings.host)
        w.port_spin.setValue(settings.port)
        w.database_edit.setText(settings.dbname)
        w.user_edit.setText(settings.user)
        w.password_edit.clear()
        w._transaction_inspected((TEXT, inspect_transaction(TEXT)))
        with psycopg.connect(**settings.connection_kwargs()) as c:
            before = c.execute('''SELECT e.enrollment_id,e.status,COUNT(sub.submission_id)
                FROM university.enrollments e LEFT JOIN university.submissions sub ON sub.enrollment_id=e.enrollment_id
                WHERE e.enrollment_id BETWEEN 17001 AND 17020 GROUP BY e.enrollment_id,e.status ORDER BY e.enrollment_id''').fetchall()
        with patch('pqo.transaction_desktop.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            w.transaction_plan_button.click()
            self.drain()
            self.assertIn('Пароль не введён', w.transaction_status.text())
            with patch('pqo.transaction_desktop.QInputDialog.getText', return_value=(settings.password, True)):
                w.transaction_password_button.click()
                self.drain()
        report = w.transaction_plan_report
        self.assertIsNotNone(report, w.transaction_status.text())
        self.assertEqual([s.status for s in report.statements], ['planned','control','planned','planned','control','control','planned','planned'])
        with psycopg.connect(**settings.connection_kwargs()) as c:
            after = c.execute('''SELECT e.enrollment_id,e.status,COUNT(sub.submission_id)
                FROM university.enrollments e LEFT JOIN university.submissions sub ON sub.enrollment_id=e.enrollment_id
                WHERE e.enrollment_id BETWEEN 17001 AND 17020 GROUP BY e.enrollment_id,e.status ORDER BY e.enrollment_id''').fetchall()
        self.assertEqual(before, after)
