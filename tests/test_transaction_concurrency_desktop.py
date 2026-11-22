from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_concurrency import (
    ConcurrentCommand, ConcurrentReplay, ConcurrentSession, LockObservation,
    concurrency_request_digest, prepare_concurrent,
)
from pqo.transaction_context import TransactionOptions
from test_transaction_concurrency import EXAMPLES


class ConcurrencyDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.prefs = QSettings(str(self.directory / 'settings.ini'), QSettings.Format.IniFormat)
        self.prefs.setValue('db_name', 'source')
        self.prefs.setValue('allowed_schemas', 'university')
        patcher = patch('pqo.desktop.QSettings', return_value=self.prefs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = w = MainWindow()
        self.addCleanup(w.close)
        w.concurrency_reference.setText('baseline')
        w.concurrency_confirm.setChecked(True)
        self.load('lock')

    def load(self, case):
        w = self.window
        for key, editor in (('sql_a', w.concurrency_a), ('sql_b', w.concurrency_b), ('schedule', w.concurrency_schedule)):
            editor.setPlainText(EXAMPLES[case][key])

    def report(self):
        w = self.window
        a, b, schedule, settings, ref, limit = w._concurrency_snapshot()
        _, steps = prepare_concurrent(a, b, schedule, settings)
        sessions = (ConcurrentSession('A', 'committed', TransactionOptions('READ COMMITTED', False, False), 10.,
            (ConcurrentCommand(1, 'select', 'completed', 1., 2, (('id', 23),), 'a' * 64),
             ConcurrentCommand('END', 'commit', 'completed', 1.))),
            ConcurrentSession('B', 'aborted', TransactionOptions('READ COMMITTED', False, False), 8.,
                (ConcurrentCommand(2, 'update', 'conflict', 3., sqlstate='40P01'),
                 ConcurrentCommand('END', 'commit', 'skipped', 0.))))
        return ConcurrentReplay('completed', ref, concurrency_request_digest(a, b, schedule, settings, ref, limit),
            steps, sessions, (LockObservation('B', 2, 'transactionid', ('A',), 3, 1., 3.),),
            (), (), (), (), True, ('pqo_replay_fake',), (), ('Автоматических повторов не было.',), None)

    def test_workspace_has_two_editors_examples_and_explicit_copy_gate(self):
        w = self.window
        self.assertEqual(w.transaction_views.tabText(11), 'Параллельные сессии')
        self.assertTrue(w.concurrency_run_button.isEnabled())
        w.concurrency_confirm.setChecked(False)
        self.assertFalse(w.concurrency_run_button.isEnabled())
        before = w.transaction_editor.toPlainText()
        with patch('pqo.transaction_concurrency_desktop.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            w.concurrency_example.setCurrentIndex(w.concurrency_example.findData('deadlock'))
            w.concurrency_example_button.click()
        self.assertEqual(w.concurrency_a.toPlainText(), EXAMPLES['deadlock']['sql_a'])
        self.assertEqual(w.concurrency_schedule.toPlainText(), EXAMPLES['deadlock']['schedule'])
        self.assertEqual(w.transaction_editor.toPlainText(), before)
        w._workflow_copy_fields('new_baseline')
        self.assertEqual(w.concurrency_reference.text(), 'new_baseline')
        self.assertFalse(w.concurrency_confirm.isChecked())

    def test_busy_gate_prevents_double_start_and_error_report_does_not_claim_two_commits(self):
        w = self.window
        snapshot, report = w._concurrency_snapshot(), self.report()
        with patch('pqo.transaction_concurrency_desktop.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), patch.object(w, '_start_worker') as start:
            w._start_concurrency()
            self.assertEqual(start.call_count, 1)
            self.assertFalse(w.analyze_button.isEnabled() or w.deep_analyze_button.isEnabled() or w.concurrency_run_button.isEnabled())
            self.assertTrue(w.concurrency_b.isReadOnly())
            self.assertTrue(w.transaction_cancel_button.isEnabled())
            w._start_concurrency()
            self.assertEqual(start.call_count, 1)
            w._concurrency_ready((snapshot, report))
        self.assertIs(w.transaction_concurrency_report, report)
        self.assertTrue(w.analyze_button.isEnabled() and w.concurrency_run_button.isEnabled())
        self.assertIn('40P01', w.concurrency_status.text())
        self.assertIn('B: отменена', w.concurrency_details.toPlainText())
        self.assertNotIn('B: COMMIT выполнен', w.concurrency_details.toPlainText())
        self.assertIn('a' * 64, w.concurrency_details.toPlainText())
        self.assertEqual(w.concurrency_commands.rowCount(), 4)
        self.assertEqual(w.concurrency_locks.rowCount(), 1)

    def test_edit_endpoint_or_new_copy_invalidates_results_and_stale_callback_is_rejected(self):
        w = self.window
        snapshot, report = w._concurrency_snapshot(), self.report()
        w._concurrency_ready((snapshot, report))
        w.concurrency_timeout.setValue(6000)
        self.assertIsNone(w.transaction_concurrency_report)
        w._concurrency_ready((snapshot, report))
        self.assertIsNone(w.transaction_concurrency_report)
        self.assertIn('Устаревший', w.concurrency_status.text())
        w._concurrency_ready((w._concurrency_snapshot(), self.report()))
        w._clear_concurrency()
        w._concurrency_ready((w._concurrency_snapshot(), replace(self.report(), request_digest='wrong')))
        self.assertIsNone(w.transaction_concurrency_report)
        w._concurrency_ready((w._concurrency_snapshot(), self.report()))
        w.database_edit.setText('other')
        self.assertIsNone(w.transaction_concurrency_report)
        self.assertEqual(w.concurrency_commands.rowCount(), 0)

    def test_json_export_is_explicit_and_preserves_conflicts_without_sql(self):
        w = self.window
        w._concurrency_ready((w._concurrency_snapshot(), self.report()))
        target = self.directory / 'report.json'
        with patch('pqo.transaction_concurrency_desktop.QFileDialog.getSaveFileName', return_value=(str(target), 'JSON')):
            w.concurrency_save_button.click()
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['sessions'][1]['outcome'], 'aborted')
        self.assertEqual(data['sessions'][1]['commands'][0]['sqlstate'], '40P01')
        self.assertNotIn('UPDATE university', target.read_text(encoding='utf-8'))
        self.assertFalse(any('sql' in k.lower() for k in self.prefs.allKeys()))

    def test_run_controls_stay_visible_when_switching_sql_and_results(self):
        w = self.window
        w.resize(1280, 900)
        w.show()
        w.tabs.setCurrentIndex(next(i for i in range(w.tabs.count()) if w.tabs.tabText(i) == 'Транзакции'))
        w.transaction_workspace_mode.setCurrentIndex(w.transaction_workspace_mode.findData('analysis'))
        w.transaction_views.setCurrentWidget(w.transaction_concurrency_page)
        original = w.concurrency_a.toPlainText()
        for mode in ('split', 'sql', 'result'):
            w.concurrency_view.setCurrentIndex(w.concurrency_view.findData(mode))
            for _ in range(3):
                self.app.processEvents()
            self.assertLessEqual(w.concurrency_run_button.geometry().bottom(), w.transaction_concurrency_page.height())
            self.assertTrue(w.concurrency_run_button.isVisible())
            self.assertEqual(w.concurrency_a.toPlainText(), original)
