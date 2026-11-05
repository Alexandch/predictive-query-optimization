import os
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_indexes import ExistingIndex, RelationIndexes, build_transaction_index_report
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport
from pqo.transaction_replay import CommandReplay, ReplaySample, StateDigest, TransactionReplay
from pqo.transaction_mixed_recommendation import replay_request_digest

TEXT = 'BEGIN; SELECT id,value FROM university.items WHERE id=1; UPDATE university.items SET value=2 WHERE id=1; SELECT id,value FROM university.items WHERE id=1; COMMIT;'


class TransactionReplayDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        prefs = QSettings(str(Path(temporary.name)/'prefs.ini'),QSettings.Format.IniFormat)
        prefs.setValue('db_name','source')
        prefs.setValue('allowed_schemas','university')
        patcher=patch('pqo.desktop.QSettings',return_value=prefs); patcher.start(); self.addCleanup(patcher.stop)

    def fixture(self,window):
        window.transaction_editor.setPlainText(TEXT)
        settings = window._database_settings()
        inspection = inspect_transaction(TEXT)
        plans = TransactionPlanReport(inspection,settings.dbname,tuple(StatementPlan(item.number,'planned','fixture',[{'Plan':{'Node Type':'Seq Scan'}}]) for item in inspection.statements))
        relation=RelationIndexes('university','items',('id','value'),(ExistingIndex('pk',('id',)),))
        indices=build_transaction_index_report(inspection,plans,{('university','items'):relation})
        window._transaction_inspected((TEXT,inspection))
        window._transaction_planned((TEXT,settings,plans))
        window._transaction_indexes_ready((TEXT,settings,indices))
        row=next(n for n,item in enumerate(indices.candidates) if item.status!='covered')
        window.transaction_index_table.item(row,0).setCheckState(Qt.CheckState.Checked)
        action=indices.candidates[row].action
        window.transaction_replay_reference.setText('lab')
        window.transaction_replay_series.setValue(1)
        window.transaction_replay_confirm.setChecked(True)
        commands=(CommandReplay(1,'select',1.,1,(('id',23),),'hash'),)
        samples=tuple(ReplaySample(1,phase,'pqo_replay_'+phase,commands,3.,(StateDigest('university.items',1,'start'),),
            (StateDigest('university.items',1,'end'),),1024 if phase=='B' else 0,1.) for phase in ('A1','B','A2'))
        report=TransactionReplay('completed','lab',(action,),samples,1,True,True,True,True,True,('pqo_replay_owned',),(),('fixture',),
            request_digest=replay_request_digest(TEXT,settings,'lab',(action,),1,window._replay_policy()),policy=window._replay_policy())
        return settings,indices,action,report

    def drain(self,window):
        deadline=time.monotonic()+5
        while window._active_workers and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.005)
        self.assertFalse(window._active_workers)

    def test_real_queued_worker_locks_all_actions_and_preserves_original(self):
        window=MainWindow()
        try:
            settings,indices,action,report=self.fixture(window)
            with patch('pqo.transaction_replay_desktop.run_transaction_replay',return_value=report) as run, patch('pqo.transaction_replay_desktop.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_replay()
                self.assertFalse(window.analyze_button.isEnabled())
                self.assertFalse(window.transaction_replay_button.isEnabled())
                self.assertFalse(window.transaction_replay_gain_ms.isEnabled())
                self.assertFalse(window.transaction_replay_gain_percent.isEnabled())
                self.assertFalse(window.transaction_replay_size_mib.isEnabled())
                self.assertTrue(window.transaction_cancel_button.isEnabled())
                window._start_transaction_replay()
                self.drain(window)
            run.assert_called_once()
            self.assertEqual(window.transaction_editor.toPlainText(),TEXT)
            self.assertIs(window.transaction_index_report,indices)
            self.assertIs(window.transaction_replay_report,report)
            self.assertEqual(window.transaction_replay_table.rowCount(),3)
            self.assertIn('НЕ РАЗРЕШЕНИЕ НА PRODUCTION',window.transaction_details.toPlainText())
            self.assertTrue(window.analyze_button.isEnabled())
        finally:
            window.close()

    def test_confirmation_and_unsupported_sql_do_not_launch_backend(self):
        window=MainWindow()
        try:
            self.fixture(window)
            with patch('pqo.transaction_replay_desktop.run_transaction_replay') as run:
                window.transaction_replay_confirm.setChecked(False)
                window._start_transaction_replay()
                self.assertFalse(window._active_workers)
            run.assert_not_called()
            self.fixture(window)
            window.transaction_editor.setPlainText(TEXT.replace('COMMIT','ROLLBACK'))
            with patch('pqo.transaction_replay_desktop.run_transaction_replay') as run:
                window._start_transaction_replay()
            run.assert_not_called()
        finally:
            window.close()

    def test_stale_callback_does_not_attach_but_unverified_cleanup_is_visible(self):
        from dataclasses import replace
        window=MainWindow()
        try:
            settings,indices,action,report=self.fixture(window)
            window.transaction_replay_reference.setText('other_lab')
            report=replace(report,cleanup_verified=False,leftover_databases=('pqo_replay_owned',))
            window._transaction_replay_ready((TEXT,TEXT,settings,'lab',1,(action,),indices,report))
            self.assertIsNone(window.transaction_replay_report)
            self.assertIn('НЕ подтверждено',window.transaction_status.text())
            self.assertIn('pqo_replay_owned',window.transaction_status.text())
        finally:
            window.close()

    def test_reference_series_candidates_and_sql_invalidate_old_execution(self):
        for change in ('reference','series','candidate','sql','gain','percent','budget'):
            window=MainWindow()
            try:
                settings,indices,action,report=self.fixture(window)
                window._transaction_replay_ready((TEXT,TEXT,settings,'lab',1,(action,),indices,report))
                self.assertIsNotNone(window.transaction_replay_report)
                if change=='reference': window.transaction_replay_reference.setText('another')
                elif change=='series': window.transaction_replay_series.setValue(2)
                elif change=='candidate':
                    for row in range(window.transaction_index_table.rowCount()):
                        window.transaction_index_table.item(row,0).setCheckState(Qt.CheckState.Unchecked)
                elif change=='gain': window.transaction_replay_gain_ms.setValue(6.)
                elif change=='percent': window.transaction_replay_gain_percent.setValue(6.)
                elif change=='budget': window.transaction_replay_size_mib.setValue(63)
                else: window.transaction_editor.setPlainText(TEXT+' -- edited')
                self.assertIsNone(window.transaction_replay_report)
                self.assertEqual(window.transaction_replay_effect_table.rowCount(),0)
                self.assertNotIn('Решение:',window.transaction_details.toPlainText())
            finally:
                window.close()

    def test_changed_policy_during_worker_rejects_queued_result(self):
        window=MainWindow()
        try:
            settings,indices,action,report=self.fixture(window)
            window.transaction_replay_gain_ms.setValue(7.)
            window._transaction_replay_ready((TEXT,TEXT,settings,'lab',1,(action,),indices,report))
            self.assertIsNone(window.transaction_replay_report)
            self.assertIn('Устаревший',window.transaction_status.text())
        finally:
            window.close()

    def test_mixed_summary_shows_total_read_write_and_command_effects(self):
        from dataclasses import replace
        from pqo.transaction_mixed_recommendation import MixedMetric, MixedRecommendation
        window=MainWindow()
        try:
            settings,indices,action,report=self.fixture(window)
            recommendation=MixedRecommendation('not_recommended',report.policy,report.request_digest,(),
                (MixedMetric('Транзакция',100.,80.),MixedMetric('SELECT',90.,50.),MixedMetric('DML',10.,30.)),
                1024,20.,('Запись замедлена.',))
            report=replace(report,recommendation=recommendation)
            window._transaction_replay_ready((TEXT,TEXT,settings,'lab',1,(action,),indices,report))
            self.assertIn('Набор не рекомендован',window.transaction_replay_status.text())
            self.assertEqual(window.transaction_replay_effect_table.rowCount(),3)
            self.assertIn('-20.000',window.transaction_replay_effect_table.item(2,3).text())
            self.assertIn('Запись замедлена',window.transaction_details.toPlainText())
            self.assertEqual(window.transaction_editor.toPlainText(),TEXT)
        finally:
            window.close()
