from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_context import TransactionOptions
from pqo.transaction_replay import CommandReplay, ReplaySample, StateDigest
from pqo.transaction_resources import ProfileSample, ResourceCommand, ResourceVariant, TransactionResources, parse_resources, resource_request_digest
from test_transaction_resources import document


TEXT = '''BEGIN;UPDATE university.students SET campus_id=campus_id+1 WHERE student_id=1;
    SELECT student_id,campus_id FROM university.students WHERE student_id=1;COMMIT;'''


class ResourceDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.prefs = QSettings(str(self.directory/'settings.ini'),QSettings.Format.IniFormat)
        self.prefs.setValue('db_name','source')
        self.prefs.setValue('allowed_schemas','university')
        p = patch('pqo.desktop.QSettings',return_value=self.prefs)
        p.start();self.addCleanup(p.stop)
        self.window = w = MainWindow();self.addCleanup(w.close)
        w.transaction_editor.setPlainText(TEXT)
        w.resource_reference.setText('baseline')
        w.resource_confirm.setChecked(True)

    def report(self):
        w = self.window
        _, text, settings, mode, actions, number, variant, reference, memory = w._resource_snapshot()
        state = (StateDigest('university.students',2,'a'*64),)
        opts = TransactionOptions('READ COMMITTED',False,False)
        control = ReplaySample(1,'A1','pqo_replay_normal',(CommandReplay(1,'update',1.,1,(),None),
            CommandReplay(2,'select',1.,1,(('id',23),),'a'*64)),10.,state,state,0,0.,0,opts)
        metric = parse_resources(document(**{'Shared Hit Blocks':12,'WAL Bytes':1024}))
        profile = ProfileSample('pqo_replay_profile',8192,4096,
            (ResourceCommand(1,'update',metric,False),ResourceCommand(2,'select',metric,True)),
            metric.counters,None,None,state,state,(),(),opts,'commit',0,0.,True,True)
        return TransactionResources('completed',reference,resource_request_digest(text,settings,reference,actions,number,variant,memory),
            4096,(ResourceVariant('original',control,profile),),None,True,True,
            ('pqo_replay_normal','pqo_replay_profile'),(),('Память одного оператора, не пиковая ОЗУ транзакции.',),None)

    def test_original_profile_needs_copy_consent_and_other_modes_need_candidates(self):
        w = self.window
        self.assertEqual(w.transaction_views.tabText(12),'Ресурсы транзакции')
        self.assertTrue(w.resource_run_button.isEnabled())
        w.resource_confirm.setChecked(False)
        self.assertFalse(w.resource_run_button.isEnabled())
        w.resource_confirm.setChecked(True)
        w.resource_mode.setCurrentIndex(w.resource_mode.findData('indexes'))
        with self.assertRaises(ValueError):w._resource_snapshot()
        w.resource_mode.setCurrentIndex(w.resource_mode.findData('rewrite'))
        with self.assertRaises(ValueError):w._resource_snapshot()
        w._workflow_copy_fields('new_baseline')
        self.assertEqual(w.resource_reference.text(),'new_baseline')
        self.assertFalse(w.resource_confirm.isChecked())

    def test_busy_gate_and_explicit_unknown_memory_are_preserved(self):
        w = self.window
        snapshot, report = w._resource_snapshot(), self.report()
        with patch('pqo.transaction_resources_desktop.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes), patch.object(w,'_start_worker') as start:
            w._start_resource_profile()
            self.assertFalse(w.analyze_button.isEnabled() or w.concurrency_run_button.isEnabled() or w.resource_run_button.isEnabled())
            self.assertTrue(w.transaction_cancel_button.isEnabled())
            w._start_resource_profile()
            self.assertEqual(start.call_count,1)
            w._resource_profile_ready((snapshot,report))
        self.assertIs(w.transaction_resource_report,report)
        self.assertTrue(w.resource_run_button.isEnabled())
        self.assertEqual(w.resource_summary.item(0,4).text(),'—')
        self.assertEqual(w.resource_summary.item(0,3).text(),'1.000')
        self.assertEqual(w.resource_commands.rowCount(),2)
        self.assertIn('не пиковая ОЗУ',w.resource_details.toPlainText())
        self.assertIn('Значения результатов EXPLAIN не возвращает',w.resource_details.toPlainText())

    def test_changes_or_wrong_provenance_invalidate_and_do_not_attach_result(self):
        w = self.window
        snapshot, report = w._resource_snapshot(), self.report()
        w._resource_profile_ready((snapshot,report))
        w.resource_work_mem.setValue(64)
        self.assertIsNone(w.transaction_resource_report)
        w._resource_profile_ready((snapshot,report))
        self.assertIsNone(w.transaction_resource_report)
        self.assertIn('Устаревший',w.resource_status.text())
        w._resource_profile_ready((w._resource_snapshot(),replace(self.report(),request_digest='wrong')))
        self.assertIsNone(w.transaction_resource_report)
        w._resource_profile_ready((w._resource_snapshot(),self.report()))
        w.transaction_editor.setPlainText(TEXT.replace('=1','=2'))
        self.assertIsNone(w.transaction_resource_report)
        w._resource_profile_ready((w._resource_snapshot(),self.report()))
        w.database_edit.setText('other')
        self.assertIsNone(w.transaction_resource_report)

    def test_export_is_explicit_and_contains_counters_without_raw_plan(self):
        w = self.window
        w._resource_profile_ready((w._resource_snapshot(),self.report()))
        target = self.directory/'resources.json'
        with patch('pqo.transaction_resources_desktop.QFileDialog.getSaveFileName',return_value=(str(target),'JSON')):
            w.resource_save_button.click()
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['variants'][0]['profile']['counters']['wal_bytes'],1024)
        self.assertFalse(data['variants'][0]['profile']['result_values_verified'])
        self.assertNotIn('UPDATE university',target.read_text(encoding='utf-8'))
        self.assertNotIn('Filter',target.read_text(encoding='utf-8'))
