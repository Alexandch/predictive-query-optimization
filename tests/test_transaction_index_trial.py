import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_indexes import RelationIndexes, ExistingIndex
from pqo.transaction_index_trial import (
    LAB_DATABASE, TrialReading, TransactionIndexTrial, assess_readings,
    run_transaction_index_trial, create_university_lab, _read_slice, _measure_phase,
)


class TransactionIndexTrialTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname="source_db", allowed_schemas=frozenset({"university"}))
        self.action = IndexAction.create("university", "enrollments", ("student_id",))
        self.text = "BEGIN; SELECT enrollment_id FROM university.enrollments WHERE student_id=1; UPDATE university.enrollments SET status='completed' WHERE student_id=1; SELECT 1; ROLLBACK;"

    def reading(self, number=1, before=100, indexed=10, restored=100, used=True):
        return TrialReading(number, (before,) * 3, (indexed,) * 3, (restored,) * 3, used)

    def test_total_gain_cannot_hide_individual_regression(self):
        readings = (self.reading(1, 100, 10), self.reading(2, 10, 20, 10, False))
        reasons = assess_readings(readings, minimum_gain_ms=1, minimum_gain_ratio=.05)
        self.assertTrue(any("SELECT 2" in reason and "регрессия" in reason for reason in reasons))

    def test_drift_unused_index_and_both_gain_thresholds(self):
        cases = ((self.reading(restored=150), "дрейф"),
                 (self.reading(used=False), "не использовал"),
                 (self.reading(indexed=99.5), "порог"))
        for item, expected in cases:
            with self.subTest(expected=expected):
                reasons = assess_readings((item,), minimum_gain_ms=1, minimum_gain_ratio=.05)
                self.assertTrue(any(expected in reason for reason in reasons))
        self.assertFalse(assess_readings((self.reading(),), minimum_gain_ms=1, minimum_gain_ratio=.05))

    def test_source_database_or_unconfirmed_copy_never_connects(self):
        for target, confirmation in (("source_db", True), ("SOURCE_DB", True), ("lab", False), ("", True)):
            with self.subTest(target=target), patch("pqo.transaction_index_trial.psycopg.connect") as connect, self.assertRaises(ValueError):
                run_transaction_index_trial(self.text, self.settings, target, self.action, test_copy_confirmed=confirmation)
            connect.assert_not_called()

    def test_writes_and_modifying_ctes_are_not_measured(self):
        text = self.text.replace("SELECT 1", "WITH gone AS (DELETE FROM university.enrollments RETURNING *) SELECT * FROM gone")
        queries, writes = _read_slice(text, self.settings, self.action)
        self.assertEqual(writes, (2, 3))
        self.assertEqual([number for number, _ in queries], [1])
        self.assertTrue(all("DELETE" not in query and "UPDATE" not in query for _, query in queries))

    def test_unsupported_context_functions_locks_parameters_and_into_rejected_before_connect(self):
        for body in ("SET work_mem='1MB'; SELECT * FROM university.enrollments",
                     "SELECT custom_function() FROM university.enrollments",
                     "SELECT * FROM university.enrollments FOR UPDATE",
                     "SELECT * FROM university.enrollments WHERE student_id=$1",
                     "SELECT * FROM other.enrollments"):
            with self.subTest(body=body), patch("pqo.transaction_index_trial.psycopg.connect") as connect, self.assertRaises(ValueError):
                run_transaction_index_trial(f"BEGIN; {body}; COMMIT;", self.settings, "lab", self.action, test_copy_confirmed=True)
            connect.assert_not_called()
        with self.assertRaises(ValueError):
            _read_slice("BEGIN; SELECT * INTO new_table FROM university.enrollments; COMMIT;", self.settings, self.action)

    def test_measurement_phase_is_read_only_and_rollback_only(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ([{"Execution Time": 10, "Plan": {"Index Name": "trial"}}],)
        with patch("pqo.transaction_index_trial.time.monotonic", return_value=1):
            samples, used = _measure_phase(connection, [(1, "SELECT 1")], 3, self.settings, 100, AnalysisControl(), "B", "trial")
        self.assertEqual(samples, {1: [10., 10., 10.]})
        self.assertEqual(used, {1})
        commands = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(commands[0], "SET LOCAL transaction_read_only = on")
        self.assertEqual(sum(command.startswith("EXPLAIN (ANALYZE") for command in commands), 4)
        connection.transaction.assert_called_once_with(force_rollback=True)

    def fake_connection(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        def execute(statement, *args):
            result = MagicMock()
            if isinstance(statement, str) and "current_database()" in statement:
                result.fetchone.return_value = ("lab",)
            elif isinstance(statement, str) and "pg_try_advisory" in statement:
                result.fetchone.return_value = (True,)
            elif isinstance(statement, str) and "SELECT c.relkind" in statement:
                result.fetchone.return_value = ("r",)
            elif isinstance(statement, str) and "pg_relation_size" in statement:
                result.fetchone.return_value = (1024,)
            return result
        connection.execute.side_effect = execute
        return connection

    def test_trial_connects_only_to_copy_and_rolls_back_index_with_independent_verification(self):
        connection = self.fake_connection()
        phase = [({1: [100] * 3, 3: [1] * 3}, set()),
                 ({1: [10] * 3, 3: [1] * 3}, {1}),
                 ({1: [100] * 3, 3: [1] * 3}, set())]
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection) as connect, \
             patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id",))), \
             patch("pqo.transaction_index_trial._measure_phase", side_effect=phase) as measure, \
             patch("pqo.transaction_index_trial._verify_absent", return_value=True) as verify:
            report = run_transaction_index_trial(self.text, self.settings, "lab", self.action, test_copy_confirmed=True)
        self.assertTrue(report.accepted_for_reads)
        self.assertTrue(report.rollback_verified)
        self.assertEqual(connect.call_args.kwargs["dbname"], "lab")
        self.assertEqual(measure.call_count, 3)
        self.assertEqual([number for number, _ in measure.call_args.args[1]], [1, 3])
        self.assertTrue(all(call.kwargs == {"force_rollback": True} for call in connection.transaction.call_args_list))
        self.assertEqual(verify.call_args.args[0].dbname, "lab")
        # The only DDL is our composed CREATE; no source DML or COMMIT is sent.
        commands = [call.args[0] for call in connection.execute.call_args_list if isinstance(call.args[0], str)]
        self.assertFalse(any(command.startswith(("UPDATE", "DELETE", "INSERT", "COMMIT")) for command in commands))

    def test_error_or_cancellation_during_b_never_accepts_and_verifies_cleanup(self):
        for error in (ValueError("injected"), AnalysisCancelled("cancel")):
            connection = self.fake_connection()
            with self.subTest(error=error), patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
                 patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id",))), \
                 patch("pqo.transaction_index_trial._measure_phase", side_effect=[({1: [100] * 3}, set()), error]), \
                 patch("pqo.transaction_index_trial._verify_absent", return_value=True) as verify:
                report = run_transaction_index_trial(self.text, self.settings, "lab", self.action, test_copy_confirmed=True)
            self.assertFalse(report.accepted_for_reads)
            self.assertTrue(report.rollback_verified)
            self.assertIn(report.status, {"error", "cancelled"})
            verify.assert_called_once()

    def test_unverified_rollback_never_returns_accepted(self):
        connection = self.fake_connection()
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id",))), \
             patch("pqo.transaction_index_trial._measure_phase", side_effect=[({1: [100] * 3, 3: [1] * 3}, set()),
                 ({1: [10] * 3, 3: [1] * 3}, {1}), ({1: [100] * 3, 3: [1] * 3}, set())]), \
             patch("pqo.transaction_index_trial._verify_absent", return_value=False):
            report = run_transaction_index_trial(self.text, self.settings, "lab", self.action, test_copy_confirmed=True)
        self.assertFalse(report.accepted_for_reads)
        self.assertFalse(report.rollback_verified)
        self.assertTrue(any("не подтверждено" in reason for reason in report.reasons))

    def test_existing_index_declines_without_ddl(self):
        connection = self.fake_connection()
        relation = RelationIndexes("university", "enrollments", ("student_id",), (ExistingIndex("existing", ("student_id",)),))
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_index_trial._load_relation_indexes", return_value=relation), \
             patch("pqo.transaction_index_trial._measure_phase") as measure:
            report = run_transaction_index_trial(self.text, self.settings, "lab", self.action, test_copy_confirmed=True)
        self.assertEqual(report.status, "declined")
        measure.assert_not_called()
        self.assertTrue(all(isinstance(call.args[0], str) for call in connection.execute.call_args_list))

    def test_write_only_candidate_declines_before_measurements_or_index_creation(self):
        action = IndexAction.create("university", "enrollments", ("offering_id",))
        text = "BEGIN; SELECT offering_id FROM university.enrollments WHERE student_id=1; UPDATE university.enrollments SET status='active' WHERE offering_id=2; ROLLBACK;"
        connection = self.fake_connection()
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id", "offering_id"))), \
             patch("pqo.transaction_index_trial._measure_phase") as measure:
            report = run_transaction_index_trial(text, self.settings, "lab", action, test_copy_confirmed=True)
        self.assertEqual(report.status, "declined")
        self.assertFalse(report.accepted_for_reads)
        self.assertFalse(report.lookup_reads)
        self.assertIn("польза для записи не измерялась", " ".join(report.reasons))
        measure.assert_not_called()
        self.assertTrue(all(isinstance(call.args[0], str) for call in connection.execute.call_args_list))

    def test_copy_metadata_rebinds_unqualified_join_columns_before_trial(self):
        text = "BEGIN; SELECT e.enrollment_id FROM university.enrollments e JOIN university.students s ON true WHERE student_id=1; ROLLBACK;"
        relation = RelationIndexes("university", "enrollments", ("student_id", "enrollment_id"))
        other = RelationIndexes("university", "students", ("student_id",))
        connection = self.fake_connection()
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_index_trial._load_relation_indexes", side_effect=[relation, other]), \
             patch("pqo.transaction_index_trial._measure_phase") as measure:
            report = run_transaction_index_trial(text, self.settings, "lab", self.action, test_copy_confirmed=True)
        self.assertEqual(report.status, "declined")
        self.assertFalse(report.lookup_reads)
        measure.assert_not_called()

    def test_lab_creation_never_overwrites_existing_database(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (1,)
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), self.assertRaises(FileExistsError):
            create_university_lab(self.settings)
        self.assertEqual(connection.execute.call_count, 2)


@skipUnless(os.getenv("PQO_TRANSACTION_TRIAL_TESTS") == "1", "requires dedicated university index lab")
class TransactionIndexTrialPostgresTests(TestCase):
    def test_real_write_only_candidate_declines_without_running_selects_or_creating_index(self):
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        with patch("pqo.transaction_index_trial._measure_phase") as measure, patch("pqo.transaction_index_trial._verify_absent") as verify:
            report = run_transaction_index_trial(text, settings, LAB_DATABASE,
                IndexAction.create("university", "enrollments", ("offering_id",)), test_copy_confirmed=True)
        self.assertEqual(report.status, "declined")
        self.assertFalse(report.readings)
        self.assertFalse(report.lookup_reads)
        self.assertEqual(report.build_time_ms, 0)
        self.assertEqual(report.index_size_bytes, 0)
        measure.assert_not_called()
        verify.assert_not_called()  # No index CREATE was attempted.

    def test_cancelling_after_index_creation_rolls_back_the_real_index(self):
        import psycopg
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))
        control = AnalysisControl()
        control.progress = lambda message: control.cancel() if message.startswith("B с индексом") else None
        text = "BEGIN; SELECT enrollment_id FROM university.enrollments WHERE student_id=17001; COMMIT;"
        report = run_transaction_index_trial(text, settings, LAB_DATABASE,
            IndexAction.create("university", "enrollments", ("student_id",)), test_copy_confirmed=True, control=control)
        self.assertEqual(report.status, "cancelled")
        self.assertFalse(report.accepted_for_reads)
        self.assertGreater(report.index_size_bytes, 0)
        self.assertTrue(report.rollback_verified)
        with psycopg.connect(**replace(settings, dbname=LAB_DATABASE).connection_kwargs()) as connection:
            self.assertFalse(connection.execute("SELECT 1 FROM pg_catalog.pg_indexes WHERE indexname=%s", (report.index_name,)).fetchone())

    def test_real_trial_uses_index_and_leaves_both_databases_unchanged(self):
        import psycopg
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        text = text.replace("ROLLBACK;", "SELECT full_name FROM university.students WHERE student_id=17001; ROLLBACK;")
        def snapshot(name):
            with psycopg.connect(**replace(settings, dbname=name).connection_kwargs()) as connection:
                return (connection.execute("SELECT * FROM university.enrollments WHERE student_id=17001 ORDER BY enrollment_id").fetchall(),
                    connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall())
        before = {name: snapshot(name) for name in (settings.dbname, LAB_DATABASE)}
        report = run_transaction_index_trial(text, settings, LAB_DATABASE,
            IndexAction.create("university", "enrollments", ("student_id", "registered_at")), test_copy_confirmed=True, minimum_gain_ms=1)
        self.assertEqual(report.status, "measured")
        self.assertTrue(report.rollback_verified)
        self.assertEqual([item.number for item in report.readings], [1, 2, 6])
        self.assertTrue(any(item.used_trial_index for item in report.readings))
        self.assertEqual(report.skipped_writes, (3, 4, 5))
        self.assertEqual(report.lookup_reads, (1, 2))
        self.assertGreater(report.index_size_bytes, 0)
        self.assertEqual(before, {name: snapshot(name) for name in before})
