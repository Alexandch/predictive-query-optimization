import os
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from pqo.desktop import MainWindow
from pqo.transaction_context import TransactionOptions
from pqo.transaction_optimization import run_transaction_optimization
from pqo.transaction_replay import CommandReplay, ReplaySample, StateDigest
from pqo.transaction_select_rewrite import prepare_select_replacement
from test_transaction_replay_desktop import TEXT


def measured_fixture(window):
    snapshot, number, variant, actions, reference, series, policy = (
        window._optimization_snapshot()
    )
    prepared = prepare_select_replacement(snapshot[1], snapshot[2], number, variant)
    scratch = MagicMock()
    scratch.created = {}
    scratch.manifest = None

    def create(*args, **kwargs):
        name = "pqo_replay_mock_" + str(len(scratch.created))
        scratch.created[name] = (100 + len(scratch.created), 10)
        return name

    scratch.create.side_effect = create
    scratch.remove.return_value = True
    scratch.finish.return_value = (True, ())
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (1024,)

    def sample(
        settings, database, program, indexes, iteration, phase, deadline, control
    ):
        changed = program.commands != prepared.original.commands
        total = (
            35.0
            if changed and indexes
            else 55.0
            if indexes
            else 65.0
            if changed
            else 110.0
        )
        commands = tuple(
            CommandReplay(c.number, c.kind, 2.0, 1, (("id", 23),), "a" * 64)
            for c in program.commands
        )
        return ReplaySample(
            iteration,
            phase,
            database,
            commands,
            total,
            (StateDigest("university.items", 1, "b" * 64),),
            (StateDigest("university.items", 1, "c" * 64),),
            1024 if indexes else 0,
            10.0 if indexes else 0,
            1024 if indexes else 0,
            TransactionOptions("READ COMMITTED", False, False),
        )

    with (
        patch("pqo.transaction_optimization.ScratchDatabases", return_value=scratch),
        patch("pqo.transaction_optimization.psycopg.connect", return_value=connection),
        patch("pqo.transaction_optimization.replay_catalog"),
        patch(
            "pqo.transaction_optimization.read_transaction_options",
            return_value=TransactionOptions("READ COMMITTED", False, False),
        ),
        patch("pqo.transaction_optimization._sample", side_effect=sample),
    ):
        result = run_transaction_optimization(
            snapshot[1],
            snapshot[2],
            reference,
            number,
            variant,
            actions,
            series=series,
            policy=policy,
            test_copy_confirmed=True,
            disposable_databases_confirmed=True,
        )
    return result


class OptimizationDesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        prefs = QSettings(
            str(Path(temporary.name) / "prefs.ini"), QSettings.Format.IniFormat
        )
        prefs.setValue("db_name", "source")
        prefs.setValue("allowed_schemas", "university")
        patcher = patch("pqo.desktop.QSettings", return_value=prefs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def drain(self):
        deadline = time.monotonic() + 5
        while self.window._active_workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.window._active_workers)

    def prepare(self):
        from test_transaction_replay_desktop import TransactionReplayDesktopTests
        w = self.window
        TransactionReplayDesktopTests().fixture(w)
        w._start_transaction_select_rewrites()
        self.drain()
        w.transaction_select_rewrite_variant.setPlainText(
            "SELECT id,value FROM university.items WHERE id IN (1)"
        )
        w.transaction_optimization_reference.setText("lab")
        w.transaction_optimization_confirm.setChecked(True)
        return w._optimization_snapshot()

    def test_four_rows_and_queued_worker_preserve_input_and_lock_other_actions(self):
        w = self.window
        self.prepare()
        report = measured_fixture(w)
        self.assertEqual(report.best_variant, "combined")
        with (
            patch(
                "pqo.transaction_optimization_desktop.run_transaction_optimization",
                return_value=report,
            ) as run,
            patch(
                "pqo.transaction_optimization_desktop.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            w._start_transaction_optimization()
            self.assertFalse(w.analyze_button.isEnabled())
            self.assertFalse(w.transaction_optimization_button.isEnabled())
            self.assertTrue(w.transaction_select_rewrite_variant.isReadOnly())
            self.assertTrue(w.transaction_cancel_button.isEnabled())
            w._start_transaction_optimization()
            self.drain()
        run.assert_called_once()
        self.assertEqual(w.transaction_editor.toPlainText(), TEXT)
        self.assertEqual(w.transaction_optimization_table.rowCount(), 4)
        self.assertEqual(w.transaction_views.currentIndex(), 10)
        self.assertIn("Лучший", w.transaction_optimization_status.text())
        self.assertEqual(
            w.statusBar().currentMessage(), w.transaction_optimization_status.text()
        )
        self.assertTrue(w.transaction_optimization_export.isEnabled())
        self.assertTrue(w.analyze_button.isEnabled())

    def test_context_changes_invalidate_result_and_reject_old_callback(self):
        w = self.window
        for change in (
            "variant",
            "indexes",
            "reference",
            "threshold",
            "series",
            "source",
        ):
            current = self.prepare()
            report = measured_fixture(w)
            w._transaction_optimization_ready((current, report))
            self.assertIsNotNone(w.transaction_optimization_report)
            if change == "variant":
                w.transaction_select_rewrite_variant.setPlainText("broken SQL")
            elif change == "indexes":
                for row in range(w.transaction_index_table.rowCount()):
                    w.transaction_index_table.item(row, 0).setCheckState(
                        Qt.CheckState.Unchecked
                    )
            elif change == "reference":
                w.transaction_optimization_reference.setText("other")
            elif change == "threshold":
                w.transaction_optimization_gain_ms.setValue(
                    w.transaction_optimization_gain_ms.value() + 1
                )
            elif change == "series":
                w.transaction_optimization_series.setValue(1)
            else:
                w.transaction_editor.setPlainText(TEXT + " -- changed")
            self.assertIsNone(w.transaction_optimization_report)
            self.assertNotIn('Лучший',w.statusBar().currentMessage())
            w._transaction_optimization_ready((current, report))
            self.assertIsNone(w.transaction_optimization_report)
            self.assertIn("Устаревшее", w.transaction_status.text())

    def test_missing_consent_and_unsupported_variant_do_not_start_work(self):
        w = self.window
        self.prepare()
        with patch(
            "pqo.transaction_optimization_desktop.run_transaction_optimization"
        ) as run:
            w.transaction_optimization_confirm.setChecked(False)
            w._start_transaction_optimization()
            w.transaction_optimization_confirm.setChecked(True)
            w.transaction_select_rewrite_variant.setPlainText(
                "DELETE FROM university.items"
            )
            w._start_transaction_optimization()
        run.assert_not_called()
        self.assertFalse(w._active_workers)

    def test_unverified_cleanup_cannot_attach_a_positive_result(self):
        w = self.window
        current = self.prepare()
        report = measured_fixture(w)
        report = replace(
            report, cleanup_verified=False, leftover_databases=("pqo_replay_owned",)
        )
        w._transaction_optimization_ready((current, report))
        self.assertIsNone(w.transaction_optimization_report)
        self.assertFalse(w.transaction_optimization_export.isEnabled())
        self.assertIn("pqo_replay_owned", w.transaction_status.text())
