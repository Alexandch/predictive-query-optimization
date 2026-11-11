import os
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from sqlglot import parse_one
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication,QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_select_rewrite import TransactionSelectRewrite,prepare_select_replacement,select_rewrite_request_digest
from pqo.transaction_mixed_recommendation import assess_mixed_replay
from test_transaction_select_rewrite import TEXT


class TransactionSelectRewriteDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        prefs=QSettings(str(Path(temporary.name)/'prefs.ini'),QSettings.Format.IniFormat)
        prefs.setValue('db_name','source'); prefs.setValue('allowed_schemas','university')
        patcher=patch('pqo.desktop.QSettings',return_value=prefs); patcher.start(); self.addCleanup(patcher.stop)

    def drain(self,window):
        deadline=time.monotonic()+5
        while window._active_workers and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.005)
        self.assertFalse(window._active_workers)

    def prepare(self,window):
        window.transaction_editor.setPlainText(TEXT)
        window._start_transaction_select_rewrites()
        self.drain(window)
        self.assertTrue(window._transaction_select_rewrite_options)
        self.assertEqual(window.transaction_views.currentIndex(),9)
        window.transaction_select_rewrite_reference.setText('lab')
        window.transaction_select_rewrite_confirm.setChecked(True)
        return window._select_rewrite_prepared()

    def result(self,window):
        from test_transaction_mixed_recommendation import MixedRecommendationTests
        snapshot,prepared=window._select_rewrite_prepared()
        policy=window._select_rewrite_policy()
        digest=select_rewrite_request_digest(snapshot[1],snapshot[2],'lab',prepared.number,prepared.variant_sql,2,policy)
        report=MixedRecommendationTests().fixture()
        report=replace(report,actions=(),request_digest=digest,policy=policy,
            samples=tuple(replace(s,index_size_bytes=0,index_final_size_bytes=0,index_build_ms=0.) for s in report.samples))
        report=replace(report,recommendation=assess_mixed_replay(report,policy,expected_request_digest=digest,mode='rewrite'))
        return snapshot,prepared,TransactionSelectRewrite(prepared.number,report)

    def test_generation_uses_queued_worker_without_db_and_shows_side_by_side(self):
        window=MainWindow()
        try:
            with patch('pqo.transaction_replay.psycopg.connect') as connect:
                snapshot,prepared=self.prepare(window)
            connect.assert_not_called()
            self.assertEqual(window.transaction_editor.toPlainText(),TEXT)
            self.assertEqual(parse_one(window.transaction_select_rewrite_original.toPlainText(),read='postgres'),parse_one(prepared.original_sql,read='postgres'))
            self.assertEqual(parse_one(window.transaction_select_rewrite_variant.toPlainText(),read='postgres'),parse_one(prepared.variant_sql,read='postgres'))
            window._show_transaction_select_rewrite_preview()
            self.assertIn('Меняется только SELECT № 2',window.transaction_details.toPlainText())
            self.assertIn('UPDATE university.items',window.transaction_details.toPlainText())
            self.assertNotIn('--- Исходный SELECT',window.transaction_details.toPlainText())
            window.transaction_select_rewrite_diff.refresh()
            self.assertTrue(window.transaction_select_rewrite_diff.source_marks)
            self.assertTrue(window.transaction_select_rewrite_diff.variant_marks)
        finally: window.close()

    def test_real_queued_trial_locks_other_actions_and_preserves_source(self):
        window=MainWindow()
        try:
            self.prepare(window)
            _,_,result=self.result(window)
            with patch('pqo.transaction_select_rewrite_desktop.run_transaction_select_rewrite',return_value=result) as run, \
                    patch('pqo.transaction_select_rewrite_desktop.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
                window._start_transaction_select_rewrite_trial()
                self.assertFalse(window.analyze_button.isEnabled())
                self.assertFalse(window.transaction_select_rewrite_generate.isEnabled())
                self.assertFalse(window.transaction_replay_button.isEnabled())
                self.assertTrue(window.transaction_select_rewrite_variant.isReadOnly())
                self.assertTrue(window.transaction_cancel_button.isEnabled())
                window._start_transaction_select_rewrite_trial()
                self.drain(window)
            run.assert_called_once()
            self.assertIs(window.transaction_select_rewrite_report,result)
            self.assertEqual(window.transaction_editor.toPlainText(),TEXT)
            self.assertIn('рекомендована',window.transaction_select_rewrite_status.text())
            self.assertTrue(window.analyze_button.isEnabled())
        finally: window.close()

    def test_source_settings_parameters_variant_and_thresholds_invalidate_results(self):
        for change in ('source','host','parameter','variant','reference','series','gain','percent'):
            window=MainWindow()
            try:
                self.prepare(window)
                snapshot,prepared,result=self.result(window)
                window._transaction_select_rewrite_ready((snapshot,'lab',2,window._select_rewrite_policy(),prepared,result))
                self.assertIsNotNone(window.transaction_select_rewrite_report)
                if change=='source': window.transaction_editor.setPlainText(TEXT+' -- changed')
                elif change=='host': window.host_edit.setText('other-host')
                elif change=='parameter': window._transaction_parameters_changed()
                elif change=='variant': window.transaction_select_rewrite_variant.appendPlainText('-- changed')
                elif change=='reference': window.transaction_select_rewrite_reference.setText('otherlab')
                elif change=='series': window.transaction_select_rewrite_series.setValue(3)
                elif change=='gain': window.transaction_select_rewrite_gain_ms.setValue(6.)
                else: window.transaction_select_rewrite_gain_percent.setValue(6.)
                self.assertIsNone(window.transaction_select_rewrite_report)
                self.assertNotIn('решение: recommended',window.transaction_details.toPlainText())
            finally: window.close()

    def test_stale_result_keeps_cleanup_warning_without_attaching(self):
        window=MainWindow()
        try:
            self.prepare(window)
            snapshot,prepared,result=self.result(window)
            result=replace(result,measurement=replace(result.measurement,cleanup_verified=False,leftover_databases=('pqo_replay_owned',)))
            window.transaction_select_rewrite_variant.setPlainText('broken SQL!')
            window._transaction_select_rewrite_ready((snapshot,'lab',2,window._select_rewrite_policy(),prepared,result))
            self.assertIsNone(window.transaction_select_rewrite_report)
            self.assertIn('НЕ подтверждено',window.transaction_status.text())
            self.assertIn('pqo_replay_owned',window.transaction_status.text())
        finally: window.close()

    def test_no_confirmation_unsupported_sql_and_changed_order_do_not_execute(self):
        window=MainWindow()
        try:
            self.prepare(window)
            with patch('pqo.transaction_select_rewrite_desktop.run_transaction_select_rewrite') as run:
                window.transaction_select_rewrite_confirm.setChecked(False)
                window._start_transaction_select_rewrite_trial()
                window.transaction_select_rewrite_confirm.setChecked(True)
                window.transaction_select_rewrite_variant.setPlainText('DELETE FROM university.items WHERE id=1')
                window._start_transaction_select_rewrite_trial()
            run.assert_not_called()
            self.assertFalse(window._active_workers)
        finally: window.close()

    def test_unsupported_generation_and_no_auto_candidates_have_visible_feedback(self):
        window=MainWindow()
        try:
            window.transaction_editor.setPlainText('BEGIN; SELECT now(); UPDATE university.items SET value=1 WHERE id=1; COMMIT;')
            window._start_transaction_select_rewrites(); self.drain(window)
            self.assertIn('не завершена',window.transaction_select_rewrite_status.text())
            window.transaction_editor.setPlainText('BEGIN; SELECT id FROM university.items; UPDATE university.items SET value=1 WHERE id=1; COMMIT;')
            window._start_transaction_select_rewrites(); self.drain(window)
            self.assertIn('автоматических замен нет',window.transaction_select_rewrite_status.text())
            self.assertTrue(window._transaction_select_rewrite_options)
        finally: window.close()
