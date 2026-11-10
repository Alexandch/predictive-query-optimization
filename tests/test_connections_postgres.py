"""Dedicated service storage against two new sources without any pqo objects."""

import os
from dataclasses import replace
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.analysis_service import analyze_query
from pqo.config import DatabaseSettings
from pqo.connections import (
    check_service_connection,
    discover_schemas,
    prepare_service_schema,
    create_service_database,
)
from pqo.history import load_analysis_history
from pqo.sequential_recommendation import recommend_sequential_indexes

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.getenv("PQO_CONNECTION_INTEGRATION_TESTS") == "1",
    "Trusted laboratory PostgreSQL with CREATEDB required; opt in explicitly",
)
class ConnectionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_settings = DatabaseSettings.from_env()
        cls.created = {}
        token = uuid4().hex
        cls.source = replace(
            cls.admin_settings,
            dbname="pqo_conn_source_" + token,
            allowed_schemas=frozenset({"demo"}),
        )
        cls.second = replace(cls.source, dbname="pqo_conn_second_" + token)
        cls.service = replace(
            cls.source,
            dbname="pqo_conn_service_" + token,
            allowed_schemas=frozenset({"pqo"}),
        )
        try:
            with psycopg.connect(
                **cls.admin_settings.connection_kwargs(), autocommit=True
            ) as admin:
                for settings in (cls.source, cls.second):
                    admin.execute(
                        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                            sql.Identifier(settings.dbname)
                        )
                    )
                    cls.created[settings.dbname] = admin.execute(
                        "SELECT oid,datdba FROM pg_database WHERE datname=%s",
                        (settings.dbname,),
                    ).fetchone()
            create_service_database(cls.service, confirmed=True)
            with psycopg.connect(**cls.admin_settings.connection_kwargs()) as admin:
                cls.created[cls.service.dbname] = admin.execute(
                    "SELECT oid,datdba FROM pg_database WHERE datname=%s",
                    (cls.service.dbname,),
                ).fetchone()
            for settings in (cls.source, cls.second):
                with psycopg.connect(**settings.connection_kwargs()) as connection:
                    connection.execute(
                        "CREATE SCHEMA demo; CREATE TABLE demo.items(id integer PRIMARY KEY, bucket integer, amount numeric)"
                    )
                    connection.execute(
                        "INSERT INTO demo.items SELECT n,n%100,n::numeric/10 FROM generate_series(1,10000) n"
                    )
                    connection.execute("ANALYZE demo.items")
            prepare_service_schema(cls.service, confirmed=True)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(
            **cls.admin_settings.connection_kwargs(), autocommit=True
        ) as admin:
            for name, identity in cls.created.items():
                if (
                    admin.execute(
                        "SELECT oid,datdba FROM pg_database WHERE datname=%s", (name,)
                    ).fetchone()
                    != identity
                ):
                    raise AssertionError(
                        "Refusing cleanup: owned laboratory DB identity changed"
                    )
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        with psycopg.connect(**cls.admin_settings.connection_kwargs()) as verifier:
            remaining = verifier.execute(
                "SELECT datname FROM pg_database WHERE datname=ANY(%s)",
                (list(cls.created),),
            ).fetchall()
            if remaining:
                raise AssertionError(f"Laboratory cleanup not confirmed: {remaining}")

    def analyze(self, settings=None, service=None):
        return analyze_query(
            "SELECT bucket, SUM(amount) FROM demo.items GROUP BY bucket",
            ROOT / "models/xgboost/trial/xgboost_query_time.joblib",
            ROOT / "models/dqn/dqn_index_advisor.pt",
            settings or self.source,
            history_settings=service or self.service,
            recommendation_threshold_ms=1e12,
            persist=True,
        )

    def assert_source_unchanged(self):
        for settings in (self.source, self.second):
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                self.assertIsNone(
                    connection.execute("SELECT to_regnamespace('pqo')").fetchone()[0]
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*),sum(id) FROM demo.items"
                    ).fetchone(),
                    (10000, 50005000),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT indexname FROM pg_indexes WHERE schemaname='demo'"
                    ).fetchall(),
                    [("items_pkey",)],
                )

    def test_history_from_two_sources_is_central_and_attributed(self):
        first, second = self.analyze(), self.analyze(self.second)
        self.assertIsNone(first.persistence_error)
        self.assertIsNone(second.persistence_error)
        self.assertNotEqual(first.query_run_id, second.query_run_id)
        records = {r.query_run_id: r for r in load_analysis_history(self.service)}
        self.assertEqual(
            records[first.query_run_id].target_context["database"], self.source.dbname
        )
        self.assertEqual(
            records[second.query_run_id].target_context["database"], self.second.dbname
        )
        self.assertNotIn("password", records[first.query_run_id].target_context)
        self.assert_source_unchanged()

    def test_service_outage_does_not_discard_prediction_or_fallback_to_source(self):
        missing = replace(self.service, dbname="pqo_nonexistent_" + uuid4().hex)
        result = self.analyze(service=missing)
        self.assertIsNone(result.query_run_id)
        self.assertGreaterEqual(result.prediction.predicted_time_ms, 0)
        self.assertIn("OperationalError", result.persistence_error)
        self.assert_source_unchanged()

    def test_discovery_and_service_setup_are_idempotent(self):
        database, schemas = discover_schemas(self.source)
        self.assertEqual(database, self.source.dbname)
        self.assertIn(("demo", 1), schemas)
        before = len(load_analysis_history(self.service))
        prepare_service_schema(self.service, confirmed=True)
        self.assertIn(
            "уже существует", create_service_database(self.service, confirmed=True)
        )
        self.assertEqual(before, len(load_analysis_history(self.service)))
        self.assertIn("история готова", check_service_connection(self.service))
        self.assert_source_unchanged()

    def test_deep_trial_writes_only_service_and_rolls_back_source(self):
        result = recommend_sequential_indexes(
            "SELECT id,amount FROM demo.items WHERE bucket=7 ORDER BY amount",
            ROOT / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            self.source,
            repetitions=1,
            max_steps=1,
            max_candidates_per_step=1,
            persist=True,
            history_settings=self.service,
        )
        self.assertIsNone(result.persistence_error)
        self.assertIsNotNone(result.sequential_analysis_id)
        record = next(
            r
            for r in load_analysis_history(self.service)
            if r.query_run_id == result.query_run_id
        )
        self.assertEqual(record.target_context["database"], self.source.dbname)
        self.assert_source_unchanged()

    def test_reusing_analysis_id_from_other_source_is_rejected_atomically(self):
        original = self.analyze()
        result = recommend_sequential_indexes(
            "SELECT bucket, SUM(amount) FROM demo.items GROUP BY bucket",
            ROOT / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            self.second,
            repetitions=1,
            max_steps=1,
            max_candidates_per_step=1,
            minimum_baseline_time_ms=1e12,
            persist=True,
            history_settings=self.service,
            query_run_id=original.query_run_id,
        )
        self.assertIsNone(result.sequential_analysis_id)
        self.assertIn("another target", result.persistence_error)
        record = next(
            r
            for r in load_analysis_history(self.service)
            if r.query_run_id == original.query_run_id
        )
        self.assertIsNone(record.sequential_analysis_id)
        self.assertEqual(record.target_context["database"], self.source.dbname)
        self.assert_source_unchanged()
