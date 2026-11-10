"""Opt-in tests on a new, empty laboratory DB; never migrate the configured DB."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
from threading import Event
import time
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.config import DatabaseSettings
from pqo.history import load_analysis_history
from pqo.recommendation_status import (
    RecommendationStatusConflict,
    set_index_recommendation_status,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/init/013_recommendation_status.sql"


@unittest.skipUnless(os.getenv("PQO_STATUS_INTEGRATION_TESTS") == "1",
                     "set PQO_STATUS_INTEGRATION_TESTS=1 only on a trusted laboratory server with CREATEDB")
class RecommendationStatusPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = DatabaseSettings.from_env()
        cls.name = "pqo_status_test_" + uuid4().hex
        cls.settings = replace(cls.source, dbname=cls.name)
        with psycopg.connect(**cls.source.connection_kwargs(), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(cls.name)))
            cls.identity = admin.execute("SELECT oid, datdba FROM pg_database WHERE datname=%s", (cls.name,)).fetchone()
        try:
            with psycopg.connect(**cls.settings.connection_kwargs()) as connection:
                connection.execute((ROOT / "database/init/001_core_schema.sql").read_text(encoding="utf-8"))
                connection.execute(MIGRATION.read_text(encoding="utf-8"))
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(**cls.source.connection_kwargs(), autocommit=True) as admin:
            identity = admin.execute("SELECT oid, datdba FROM pg_database WHERE datname=%s", (cls.name,)).fetchone()
            if identity != cls.identity:
                raise AssertionError("Refusing cleanup: laboratory DB identity changed")
            # No FORCE and no termination of other sessions.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))
        with psycopg.connect(**cls.source.connection_kwargs(), autocommit=True) as verifier:
            if verifier.execute("SELECT 1 FROM pg_database WHERE datname=%s", (cls.name,)).fetchone():
                raise AssertionError("Laboratory database cleanup not confirmed")

    def recommendation(self, connection, status="proposed"):
        run = connection.execute(
            "INSERT INTO pqo.query_run(sql_text,sql_hash,source) VALUES ('SELECT 1',%s,'application') RETURNING id",
            ("0" * 64,),
        ).fetchone()[0]
        return connection.execute(
            "INSERT INTO pqo.index_recommendation(query_run_id,table_name,index_columns,action,status,proposed_ddl) "
            "VALUES (%s,'public.demo',ARRAY['id'],'create',%s,'CREATE TABLE public.must_not_create(id int)') RETURNING id",
            (run, status),
        ).fetchone()[0]

    def test_full_transition_matrix_and_direct_update_guard(self):
        states = ("proposed", "accepted", "rejected", "applied", "reverted")
        allowed = {("proposed", "accepted"), ("proposed", "rejected"),
                   ("accepted", "applied"), ("applied", "reverted")}
        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            for previous in states:
                for target in states:
                    with self.subTest(previous=previous, target=target), connection.transaction(force_rollback=True):
                        rec = self.recommendation(connection, previous)
                        if (previous, target) in allowed:
                            connection.execute("CALL pqo.set_recommendation_status(%s,%s,%s)", (rec, previous, target))
                            self.assertEqual(connection.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], target)
                            self.assertEqual(connection.execute("SELECT old_status,new_status,changed_by=current_user FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchall(), [(previous, target, True)])
                        else:
                            for query, params in (
                                ("CALL pqo.set_recommendation_status(%s,%s,%s)", (rec, previous, target)),
                                ("UPDATE pqo.index_recommendation SET status=%s WHERE id=%s", (target, rec)),
                            ):
                                with self.assertRaises(psycopg.errors.CheckViolation), connection.transaction():
                                    connection.execute(query, params)
                            self.assertEqual(connection.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], previous)
                            self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 0)

    def test_client_call_history_and_outer_rollback_include_audit(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            rec = self.recommendation(connection)
            connection.commit()
            try:
                with connection.transaction(force_rollback=True):
                    original_timeouts = connection.execute("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')").fetchone()
                    set_index_recommendation_status(rec, "proposed", "accepted", self.settings, connection=connection)
                    self.assertEqual(connection.execute("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')").fetchone(), original_timeouts)
                    self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 1)
                    self.assertIsNone(connection.execute("SELECT to_regclass('public.must_not_create')").fetchone()[0])
                self.assertEqual(connection.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], "proposed")
                self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 0)
                connection.rollback()
                set_index_recommendation_status(rec, "proposed", "rejected", self.settings)
                history = next(row for row in load_analysis_history(self.settings) if row.index_recommendation_id == rec)
                self.assertEqual(history.index_recommendation_status, "rejected")
            finally:
                connection.rollback()
                connection.execute("DELETE FROM pqo.query_run WHERE id=(SELECT query_run_id FROM pqo.index_recommendation WHERE id=%s)", (rec,))
                connection.commit()

    def test_missing_stale_null_and_invalid_parameters_recover_savepoint(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            with connection.transaction(force_rollback=True):
                rec = self.recommendation(connection)
                cases = ((rec, "accepted", "applied", "55000"),
                         (9223372036854775807, "proposed", "accepted", "P0002"),
                         (None, "proposed", "accepted", "22023"),
                         (0, "proposed", "accepted", "22023"),
                         (rec, None, "accepted", "22023"),
                         (rec, "proposed", None, "22023"),
                         (rec, "bad", "accepted", "22023"),
                         (rec, "proposed", "bad", "22023"))
                for ident, previous, target, state in cases:
                    with self.subTest(state=state, params=(ident, previous, target)):
                        with self.assertRaises(psycopg.Error) as raised, connection.transaction():
                            connection.execute("CALL pqo.set_recommendation_status(%s,%s,%s)", (ident, previous, target))
                        self.assertEqual(raised.exception.sqlstate, state)
                        self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
                self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 0)

    def test_concurrent_stale_decision_waits_then_does_not_overwrite(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as first, \
                psycopg.connect(**self.settings.connection_kwargs()) as second:
            rec = self.recommendation(first)
            first.commit()
            pid = second.info.backend_pid
            entered = Event()
            first.execute("CALL pqo.set_recommendation_status(%s,'proposed','accepted')", (rec,))
            def competing_decision():
                entered.set()
                return set_index_recommendation_status(rec, "proposed", "rejected", self.settings, connection=second)
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(competing_decision)
                    try:
                        self.assertTrue(entered.wait(2))
                        with psycopg.connect(**self.settings.connection_kwargs(), autocommit=True) as observer:
                            deadline = time.monotonic() + 3
                            blocked = False
                            while time.monotonic() < deadline:
                                blocked = observer.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()[0]
                                if blocked:
                                    break
                                time.sleep(.01)
                            self.assertTrue(blocked, "Second writer must actually wait for the row lock")
                    finally:
                        first.commit()
                    with self.assertRaises(RecommendationStatusConflict):
                        future.result(timeout=5)
                self.assertEqual(second.execute("SELECT 1").fetchone(), (1,))
                self.assertEqual(second.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], "accepted")
                self.assertEqual(second.execute("SELECT old_status,new_status FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchall(), [("proposed", "accepted")])
            finally:
                second.rollback()
                first.rollback()
                first.execute("DELETE FROM pqo.query_run WHERE id=(SELECT query_run_id FROM pqo.index_recommendation WHERE id=%s)", (rec,))
                first.commit()

    def test_lock_timeout_does_not_damage_caller_or_add_audit(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as first, \
                psycopg.connect(**self.settings.connection_kwargs()) as second:
            rec = self.recommendation(first)
            first.commit()
            try:
                first.execute("SELECT id FROM pqo.index_recommendation WHERE id=%s FOR UPDATE", (rec,))
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    set_index_recommendation_status(rec, "proposed", "accepted",
                                                    replace(self.settings, statement_timeout_ms=400), connection=second)
                self.assertEqual(second.execute("SELECT 1").fetchone(), (1,))
                self.assertEqual(second.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], "proposed")
                self.assertEqual(second.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 0)
            finally:
                second.rollback()
                first.rollback()
                first.execute("DELETE FROM pqo.query_run WHERE id=(SELECT query_run_id FROM pqo.index_recommendation WHERE id=%s)", (rec,))
                first.commit()

    def test_procedure_is_invoker_and_read_only_transaction_cannot_write(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            with connection.transaction(force_rollback=True):
                self.assertEqual(connection.execute("SELECT prokind,prosecdef FROM pg_proc WHERE oid='pqo.set_recommendation_status(bigint,text,text)'::regprocedure").fetchone(), ("p", False))
                rec = self.recommendation(connection)
                connection.execute("SET LOCAL TRANSACTION READ ONLY")
                with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction), connection.transaction():
                    connection.execute("CALL pqo.set_recommendation_status(%s,'proposed','accepted')", (rec,))

    def test_reapplication_and_down_migration_preserve_rows_and_audit(self):
        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            with connection.transaction(force_rollback=True):
                rec = self.recommendation(connection)
                connection.execute("CALL pqo.set_recommendation_status(%s,'proposed','accepted')", (rec,))
                connection.execute(MIGRATION.read_text(encoding="utf-8"))
                connection.execute(MIGRATION.read_text(encoding="utf-8"))
                connection.execute((ROOT / "database/migrations/013_recommendation_status.down.sql").read_text(encoding="utf-8"))
                self.assertEqual(connection.execute("SELECT status FROM pqo.index_recommendation WHERE id=%s", (rec,)).fetchone()[0], "accepted")
                self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit WHERE recommendation_id=%s", (rec,)).fetchone()[0], 1)
                with self.assertRaisesRegex(RuntimeError, "013_recommendation_status.sql"):
                    set_index_recommendation_status(rec, "accepted", "applied", self.settings, connection=connection)

    def test_defense_sql_example_rolls_back_recommendation_and_audit(self):
        with psycopg.connect(**self.settings.connection_kwargs(), autocommit=True) as connection:
            before = connection.execute("SELECT count(*) FROM pqo.query_run").fetchone()
            audit = connection.execute("SELECT count(*) FROM pqo.recommendation_audit").fetchone()
            connection.execute((ROOT / "examples/defense/04-recommendation-status.sql").read_text(encoding="utf-8"))
            self.assertEqual(connection.execute("SELECT count(*) FROM pqo.query_run").fetchone(), before)
            self.assertEqual(connection.execute("SELECT count(*) FROM pqo.recommendation_audit").fetchone(), audit)
