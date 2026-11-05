import os
from dataclasses import replace
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

import psycopg

from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.transaction_planning import collect_transaction_plans


class TransactionPlanningTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), allowed_schemas=frozenset({"university"}))
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.execute.return_value.fetchone.return_value = ([{
            "Plan": {"Node Type": "Result", "Total Cost": 1, "Plan Rows": 1}
        }],)

    def collect(self, text, **kwargs):
        with patch("pqo.transaction_planning.psycopg.connect", return_value=self.connection):
            return collect_transaction_plans(text, self.settings, **kwargs)

    def test_only_explain_not_raw_commands_or_analyze_and_always_rollback(self):
        report = self.collect("BEGIN; SELECT 1; UPDATE university.students SET campus_id=7 WHERE student_id=1; COMMIT;")
        self.assertEqual([p.status for p in report.statements], ["planned", "planned"])
        self.assertFalse(report.inspection.execution_permitted)
        commands = [call.args[0] for call in self.connection.execute.call_args_list]
        self.assertEqual(commands[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        explains = [sql for sql in commands if sql.startswith("EXPLAIN")]
        self.assertEqual(len(explains), 2)
        self.assertTrue(all(sql.startswith("EXPLAIN (FORMAT JSON) ") for sql in explains))
        self.assertTrue(all("ANALYZE" not in sql for sql in commands))
        self.assertFalse(any(sql.startswith(("UPDATE", "COMMIT", "BEGIN", "CREATE INDEX")) for sql in commands))
        self.assertTrue(all(call.kwargs == {"force_rollback": True}
                            for call in self.connection.transaction.call_args_list))

    def test_blocked_statements_never_connect(self):
        examples = ("SELECT $1", "SELECT :value", "SELECT ?", "SET work_mem='1MB'",
                    "SELECT custom_function()", "SELECT * FROM students",
                    "SELECT * FROM other.students", "SELECT * FROM university.students FOR UPDATE",
                    "SELECT 1 INTO temp x", "CREATE TABLE university.x(id int)",
                    "SELECT * FROM otherdb.university.students")
        for sql in examples:
            with self.subTest(sql=sql), patch("pqo.transaction_planning.psycopg.connect") as connect:
                report = collect_transaction_plans(f"BEGIN; {sql}; ROLLBACK;", self.settings)
                self.assertEqual(report.statements[0].status, "blocked")
                connect.assert_not_called()

    def test_cte_references_are_scoped_and_do_not_bypass_schema_validation(self):
        good = "WITH x AS (SELECT * FROM university.students) SELECT * FROM x"
        bad = "SELECT * FROM x WHERE EXISTS (WITH x AS (SELECT 1) SELECT * FROM x)"
        self.assertEqual(self.collect(f"BEGIN; {good}; COMMIT;").statements[0].status, "planned")
        self.assertEqual(self.collect(f"BEGIN; {bad}; COMMIT;").statements[0].status, "blocked")

    def test_timeout_or_sql_error_does_not_discard_following_plans(self):
        original_execute = self.connection.execute
        def execute(sql, *args):
            if sql.startswith("EXPLAIN") and "missing" in sql:
                raise psycopg.errors.UndefinedTable("missing")
            return original_execute.return_value
        self.connection.execute.side_effect = execute
        report = self.collect("BEGIN; SELECT * FROM university.missing; SELECT 1; COMMIT;")
        self.assertEqual([p.status for p in report.statements], ["error", "planned"])
        self.assertIn("42P01", report.statements[0].explanation)
        self.connection.transaction.return_value.__exit__.assert_called()

    def test_timeout_is_reported_and_cancellation_is_not_swallowed(self):
        control = AnalysisControl()
        def timeout(sql, *args):
            if sql.startswith("EXPLAIN"):
                raise psycopg.errors.QueryCanceled("timeout")
            return self.connection.execute.return_value
        self.connection.execute.side_effect = timeout
        report = self.collect("BEGIN; SELECT 1; COMMIT;")
        self.assertEqual(report.statements[0].status, "error")
        self.assertIn("statement_timeout", report.statements[0].explanation)
        control.cancel()
        with patch("pqo.transaction_planning.psycopg.connect") as connect, self.assertRaises(AnalysisCancelled):
            collect_transaction_plans("BEGIN; SELECT 1; COMMIT;", self.settings, control=control)
        connect.assert_not_called()

    def test_cancel_during_query_rolls_back_and_stops_next_command(self):
        control = AnalysisControl()
        def cancel(sql, *args):
            if sql.startswith("EXPLAIN"):
                control.cancel()
                raise psycopg.errors.QueryCanceled("cancel")
            return self.connection.execute.return_value
        self.connection.execute.side_effect = cancel
        with self.assertRaises(AnalysisCancelled):
            self.collect("BEGIN; SELECT 1; SELECT 2; COMMIT;", control=control)
        self.assertEqual(sum(call.args[0].startswith("EXPLAIN")
                             for call in self.connection.execute.call_args_list), 1)

    def test_budget_exhaustion_does_not_start_more_explains(self):
        with patch("pqo.transaction_planning.time.monotonic", side_effect=[0, 91, 92]):
            report = self.collect("BEGIN; SELECT 1; SELECT 2; COMMIT;")
        self.assertEqual([p.status for p in report.statements], ["budget_exhausted"] * 2)
        self.assertFalse(any(call.args[0].startswith("EXPLAIN")
                             for call in self.connection.execute.call_args_list))

    def test_no_eligible_sql_or_empty_transaction_is_not_executed(self):
        with patch("pqo.transaction_planning.psycopg.connect") as connect, self.assertRaises(ValueError):
            collect_transaction_plans("UPDATE university.students SET campus_id=7", self.settings)
        connect.assert_not_called()


@skipUnless(os.getenv("PQO_UNIVERSITY_TESTS") == "1", "requires isolated university test database")
class TransactionPlanningPostgresTests(TestCase):
    def test_real_blocked_plan_can_be_cancelled_without_replaying_commands(self):
        from threading import Timer
        from time import monotonic
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen",
                           allowed_schemas=frozenset({"university"}))
        control = AnalysisControl()
        with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as locker:
            with locker.transaction(force_rollback=True):
                # Test database only. NOWAIT avoids waiting for another user's session.
                locker.execute("LOCK TABLE university.students IN ACCESS EXCLUSIVE MODE NOWAIT")
                timer = Timer(.2, control.cancel)
                started = monotonic()
                timer.start()
                try:
                    with self.assertRaises(AnalysisCancelled):
                        collect_transaction_plans("BEGIN; SELECT * FROM university.students; COMMIT;",
                                                  settings, control=control)
                finally:
                    timer.cancel()
                    timer.join()
                self.assertLess(monotonic() - started, 5)

    def test_dml_is_only_planned_and_later_reads_see_initial_state(self):
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen",
                           allowed_schemas=frozenset({"university"}))
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            before = connection.execute("SELECT * FROM university.students WHERE student_id=17001").fetchall()
        report = collect_transaction_plans("""BEGIN;
            SELECT * FROM university.students WHERE student_id=17001;
            UPDATE university.students SET campus_id=999 WHERE student_id=17001;
            INSERT INTO university.students SELECT * FROM university.students WHERE student_id=17001;
            DELETE FROM university.students WHERE student_id=17001;
            SELECT * FROM university.missing_table;
            SELECT * FROM university.students WHERE student_id=17001;
            COMMIT;""", settings)
        self.assertEqual([p.status for p in report.statements],
                         ["planned", "planned", "planned", "planned", "error", "planned"])
        self.assertEqual([report.statements[i].root["Node Type"] for i in (1, 2, 3)], ["ModifyTable"] * 3)
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            after = connection.execute("SELECT * FROM university.students WHERE student_id=17001").fetchall()
        self.assertTrue(before)
        self.assertEqual(before, after)
