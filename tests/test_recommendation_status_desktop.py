import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from pqo.desktop import MainWindow
from pqo.history import HistoryRecord
from pqo.recommendation_status import IndexRecommendationStatusUpdate, RecommendationStatusConflict


class RecommendationStatusDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = QSettings(str(Path(temporary.name) / "preferences.ini"), QSettings.Format.IniFormat)
        patcher = patch("pqo.desktop.QSettings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, **changes):
        return replace(HistoryRecord(1, datetime.now(timezone.utc), "completed", "SELECT 1",
                                    10., "Result", "public.demo", ("id",), .1,
                                    index_recommendation_id=7, index_recommendation_status="proposed"), **changes)

    def show(self, window, records=None):
        window._show_history_for_connection(records or [self.record()], window._service_database_settings())
        window.history_table.selectRow(0)

    def drain(self, window):
        deadline = time.monotonic() + 5
        while window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(window._active_workers)

    def test_only_proposed_saved_candidates_can_be_decided(self):
        window = MainWindow()
        try:
            self.assertFalse(window.history_accept_index_button.isEnabled())
            for status in ("proposed", "accepted", "rejected", "applied", "reverted"):
                self.show(window, [self.record(index_recommendation_status=status)])
                self.assertEqual(window.history_accept_index_button.isEnabled(), status == "proposed")
            self.show(window, [self.record(index_recommendation_id=None)])
            self.assertFalse(window.history_reject_index_button.isEnabled())
        finally:
            window.close()

    def test_real_queued_worker_updates_metadata_once_and_blocks_duplicate(self):
        window = MainWindow()
        release = Event()
        try:
            self.show(window)
            def decide(*args):
                release.wait(3)
                return IndexRecommendationStatusUpdate(7, "proposed", "accepted")
            with patch("pqo.desktop.set_index_recommendation_status", side_effect=decide) as call:
                window.history_accept_index_button.click()
                self.assertFalse(window.history_accept_index_button.isEnabled())
                self.assertFalse(window.history_refresh_button.isEnabled())
                window._record_history_index_decision("rejected")
                release.set()
                self.drain(window)
            call.assert_called_once()
            self.assertEqual(window.history_records[0].index_recommendation_status, "accepted")
            self.assertIn("Индекс не создавался", window.history_index_decision_status.text())
            self.assertTrue(window.history_refresh_button.isEnabled())
        finally:
            release.set()
            self.drain(window)
            window.close()

    def test_stale_error_requires_refresh_and_reenables_refresh(self):
        window = MainWindow()
        try:
            self.show(window)
            with patch("pqo.desktop.set_index_recommendation_status", side_effect=RecommendationStatusConflict("Обновите историю")):
                window.history_reject_index_button.click()
                self.drain(window)
            self.assertIn("не сохранено", window.history_index_decision_status.text())
            self.assertFalse(window.history_accept_index_button.isEnabled())
            self.assertTrue(window.history_refresh_button.isEnabled())
            self.show(window)
            self.assertTrue(window.history_accept_index_button.isEnabled())
        finally:
            window.close()

    def test_changed_connection_and_invalid_settings_disable_decision(self):
        window = MainWindow()
        try:
            self.show(window)
            settings = window._service_database_settings()
            window.service_database_edit.setText("different_database")
            self.assertFalse(window.history_accept_index_button.isEnabled())
            with patch("pqo.desktop.set_index_recommendation_status") as call:
                window._record_history_index_decision("accepted")
            call.assert_not_called()
            window._history_index_decision_saved(IndexRecommendationStatusUpdate(7, "proposed", "accepted"), settings)
            self.assertEqual(window.history_records[0].index_recommendation_status, "proposed")
            self.assertIn("прежнем подключении", window.history_index_decision_status.text())
            window.service_database_edit.setText("")
            self.assertFalse(window.history_reject_index_button.isEnabled())
            with patch("psycopg.connect") as connect:
                window._refresh_history()
            connect.assert_not_called()
            self.assertIn("Проверьте настройки", window.history_index_decision_status.text())
        finally:
            window.service_database_edit.setText("pqo_service")
            window.close()

    def test_query_operation_blocks_decision_and_restores_it(self):
        window = MainWindow()
        try:
            self.show(window)
            window._set_query_actions_enabled(False)
            self.assertFalse(window.history_accept_index_button.isEnabled())
            window._set_query_actions_enabled(True)
            self.assertTrue(window.history_accept_index_button.isEnabled())
        finally:
            window.close()
