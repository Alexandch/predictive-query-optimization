import hashlib
import os
import unittest
from pathlib import Path

from pqo.analysis_service import analyze_query
from pqo.config import DatabaseSettings
from pqo.dataset import collect_sample
from pqo.explain import collect_explain
from pqo.dqn_features import collect_action_database_context
from pqo.index_actions import IndexAction
from pqo.index_environment import IndexExperimentEnvironment
from pqo.query_generator import AviationQueryGenerator


@unittest.skipUnless(
    os.getenv("PQO_INTEGRATION_TESTS") == "1",
    "set PQO_INTEGRATION_TESTS=1 to run PostgreSQL integration tests",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    def test_collects_real_explain_plan(self):
        result = collect_explain(
            "SELECT value FROM generate_series(1, 3) AS value"
        )

        self.assertGreaterEqual(result.features.node_count, 1)
        self.assertGreaterEqual(result.features.estimated_total_cost, 0)
        self.assertIsNotNone(result.features.actual_total_time_ms)

    def test_collects_action_specific_index_context(self):
        action = IndexAction.create(
            "aviation",
            "flights",
            ("flight_no", "scheduled_departure"),
        )
        context = collect_action_database_context(action)

        self.assertGreater(context["target_relation_rows"], 0)
        self.assertGreater(context["target_relation_size_bytes"], 0)
        self.assertTrue(context["exact_index_exists"])
        self.assertTrue(context["prefix_index_exists"])

    def test_pagila_functions_resolve_isolated_schema(self):
        import psycopg

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            result = connection.execute(
                "SELECT count(*) FROM pagila.film_in_stock(1, 1)"
            ).fetchone()[0]

        self.assertGreaterEqual(result, 0)

    def test_collects_estimated_plan_without_executing_query(self):
        result = collect_explain(
            "SELECT pg_sleep(1)",
            analyze=False,
        )

        self.assertIsNone(result.features.actual_total_time_ms)

    def test_function_and_audit_trigger(self):
        import psycopg

        sql_text = "SELECT 1"
        sql_hash = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
        settings = DatabaseSettings.from_env()

        with psycopg.connect(**settings.connection_kwargs()) as connection:
            query_run_id = connection.execute(
                "SELECT pqo.register_query_run(%s, %s, %s)",
                (sql_text, sql_hash, "integration-test"),
            ).fetchone()[0]
            recommendation_id = connection.execute(
                """
                INSERT INTO pqo.index_recommendation (
                    query_run_id,
                    table_name,
                    index_columns,
                    action
                ) VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (query_run_id, "bookings", ["book_ref"], "create"),
            ).fetchone()[0]
            connection.execute(
                "UPDATE pqo.index_recommendation SET status = 'accepted' WHERE id = %s",
                (recommendation_id,),
            )
            audit_row = connection.execute(
                """
                SELECT old_status, new_status
                FROM pqo.recommendation_audit
                WHERE recommendation_id = %s
                """,
                (recommendation_id,),
            ).fetchone()
            connection.rollback()

        self.assertEqual(audit_row, ("proposed", "accepted"))

    def test_collects_and_persists_complete_sample(self):
        import psycopg

        result = collect_sample("SELECT 1 AS value")
        self.assertIsNotNone(result.query_run_id)

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            stored = connection.execute(
                """
                SELECT qr.status, qf.select_expression_count, ep.node_count
                FROM pqo.query_run AS qr
                JOIN pqo.query_features AS qf ON qf.query_run_id = qr.id
                JOIN pqo.execution_plan AS ep ON ep.query_run_id = qr.id
                WHERE qr.id = %s
                """,
                (result.query_run_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM pqo.query_run WHERE id = %s",
                (result.query_run_id,),
            )

        self.assertEqual(stored[0], "completed")
        self.assertEqual(stored[1], 1)
        self.assertGreaterEqual(stored[2], 1)

    def test_all_aviation_templates_execute(self):
        generator = AviationQueryGenerator(seed=2026)
        cases = generator.generate_one_per_template()

        for case in cases:
            with self.subTest(template_id=case.template_id):
                result = collect_sample(case, persist=False)
                self.assertEqual(result.values["template_id"], case.template_id)
                self.assertIsNotNone(result.values["actual_total_time_ms"])

    def test_index_experiment_is_rolled_back(self):
        import psycopg

        action = IndexAction.create(
            "aviation", "flights", ("departure_airport", "status")
        )
        environment = IndexExperimentEnvironment(repetitions=1)
        result = environment.evaluate(
            """
            SELECT flight_id
            FROM aviation.flights
            WHERE departure_airport = 'MSQ' AND status = 'Scheduled'
            """,
            action,
        )

        self.assertGreater(result.baseline_time_ms, 0)
        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            count = connection.execute(
                """
                SELECT count(*)
                FROM pg_indexes
                WHERE schemaname = 'aviation'
                  AND indexname LIKE 'pqo_trial_%'
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_combined_analysis_is_persisted(self):
        import psycopg

        result = analyze_query(
            "SELECT flight_id FROM aviation.flights "
            "WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure",
            self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
            self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
            recommendation_threshold_ms=0,
            persist=True,
        )
        self.assertIsNotNone(result.query_run_id)
        self.assertIsNotNone(result.recommendation)

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            stored = connection.execute(
                """
                SELECT qr.status, mp.model_name, mp.predicted_time_ms
                FROM pqo.query_run AS qr
                JOIN pqo.model_prediction AS mp ON mp.query_run_id = qr.id
                WHERE qr.id = %s
                """,
                (result.query_run_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM pqo.query_run WHERE id = %s",
                (result.query_run_id,),
            )

        self.assertEqual(stored[0], "completed")
        self.assertEqual(stored[1], "xgboost")
        self.assertGreaterEqual(stored[2], 0)


if __name__ == "__main__":
    unittest.main()
