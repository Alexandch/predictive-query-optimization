import hashlib
import os
import tempfile
import unittest
from threading import Timer
from time import monotonic
from pathlib import Path

from pqo.analysis_service import analyze_query
from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.dataset import collect_sample
from pqo.explain import collect_explain
from pqo.dqn_features import collect_action_database_context
from pqo.index_actions import IndexAction
from pqo.index_environment import IndexExperimentEnvironment
from pqo.managed_indexes import apply_verified_indexes, rollback_verified_indexes
from pqo.query_generator import AviationQueryGenerator
from pqo.sequential_recommendation import recommend_sequential_indexes
from pqo.sequential_environment import SequentialIndexEnvironment
from pqo.sql_rewrite import evaluate_sql_rewrites, evaluate_sql_variant
from pqo.rewrite_journal import iter_journal, journal_summary
from pqo.structural_feedback import (
    RecommendationDecision,
    record_recommendation_decision,
    record_recommendation_measurement,
)
from pqo.structural_advisor import (
    RecommendationCategory,
    RecommendationPriority,
    StructuralRecommendation,
)
from pqo.structural_validation import (
    validate_and_save_structural_recommendation,
    validate_structural_recommendation,
)


@unittest.skipUnless(
    os.getenv("PQO_INTEGRATION_TESTS") == "1",
    "set PQO_INTEGRATION_TESTS=1 to run PostgreSQL integration tests",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def test_rewrite_journal_contains_plans_and_raw_paired_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = evaluate_sql_variant(
                "SELECT x FROM (VALUES (1),(1),(2)) v(x)",
                "SELECT 1 AS x UNION ALL SELECT 1 UNION ALL SELECT 2",
                repetitions=2, minimum_baseline_time_ms=0,
                minimum_absolute_improvement_ms=1000000,
                journal_directory=directory,
            )
            self.assertIsNotNone(plan.journal_run_id)
            self.assertIsNone(plan.journal_warning)
            run = list(iter_journal(directory))[0]
            self.assertEqual(run["status"], "completed")
            self.assertIsNotNone(run["baseline_probe"])
            self.assertGreaterEqual(run["environment"]["postgres_version"], 170000)
            candidate = run["candidates"][0]
            self.assertEqual(len(candidate["baseline_samples_ms"]), 2)
            self.assertEqual(len(candidate["variant_samples_ms"]), 2)
            self.assertEqual(len(candidate["measurements"]), 6)
            self.assertEqual(set(candidate["estimated_plans"]), {"baseline", "variant"})
            self.assertTrue(candidate["runtime_training_eligible"])
            self.assertFalse(candidate["accepted"])

    def test_rewrite_journal_preserves_baseline_timeout_without_zero_target(self):
        import psycopg
        settings = DatabaseSettings(statement_timeout_ms=30)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(psycopg.errors.QueryCanceled):
                evaluate_sql_variant("SELECT 1 FROM pg_sleep(0.2)", "SELECT 1", settings,
                                     minimum_baseline_time_ms=0, journal_directory=directory)
            run = list(iter_journal(directory))[0]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["error"]["stage"], "baseline_probe")
            self.assertTrue(run["error"]["timed_out"])
            self.assertIsNone(run["baseline_probe"])
            self.assertFalse(journal_summary(directory)[0]["runtime_training_eligible"])

    def test_rewrite_journal_distinguishes_candidate_verification_timeout(self):
        settings = DatabaseSettings(statement_timeout_ms=30)
        with tempfile.TemporaryDirectory() as directory:
            plan = evaluate_sql_variant("SELECT 1", "SELECT 1 FROM pg_sleep(0.2)", settings,
                                        minimum_baseline_time_ms=0, journal_directory=directory)
            self.assertEqual(plan.terminal_reason, "verification_failed")
            run = list(iter_journal(directory))[0]
            candidate = run["candidates"][0]
            self.assertEqual(candidate["error"]["stage"], "equivalence")
            self.assertTrue(candidate["error"]["timed_out"])
            self.assertIsNone(candidate["equivalence"])
            self.assertIsNone(candidate["rewritten_time_ms"])
            self.assertFalse(candidate["runtime_training_eligible"])

    def test_below_threshold_candidate_is_not_labeled_as_a_failed_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = evaluate_sql_variant("SELECT 1", "SELECT 2", minimum_baseline_time_ms=1000000,
                                        journal_directory=directory)
            self.assertEqual(plan.terminal_reason, "below_runtime_threshold")
            candidate = list(iter_journal(directory))[0]["candidates"][0]
            self.assertEqual(candidate["status"], "not_checked")
            self.assertIsNone(candidate["equivalence"])
            self.assertIsNone(candidate["rewritten_time_ms"])
            self.assertFalse(candidate["runtime_training_eligible"])

    def test_manual_rewrite_checks_duplicates_and_measures_both_queries(self):
        plan = evaluate_sql_variant(
            "SELECT x FROM (VALUES (1),(1),(2)) v(x)",
            "SELECT 1 AS x UNION ALL SELECT 1 UNION ALL SELECT 2",
            repetitions=1, minimum_baseline_time_ms=0,
            minimum_absolute_improvement_ms=0, minimum_improvement_ratio=0,
        )
        self.assertEqual(len(plan.evaluations), 1)
        result = plan.evaluations[0]
        self.assertTrue(result.equivalent)
        self.assertIsNotNone(result.baseline_time_ms)
        self.assertIsNotNone(result.rewritten_time_ms)

    def test_manual_rewrite_rejects_changed_multiplicity(self):
        plan = evaluate_sql_variant(
            "SELECT x FROM (VALUES (1),(1),(2)) v(x)",
            "SELECT DISTINCT x FROM (VALUES (1),(1),(2)) v(x)",
            minimum_baseline_time_ms=0,
        )
        self.assertEqual(plan.terminal_reason, "result_mismatch")
        self.assertFalse(plan.evaluations[0].equivalent)
        self.assertIsNone(plan.recommended)

    def test_reused_cte_hint_is_measured_and_equivalence_checked(self):
        plan = evaluate_sql_rewrites(
            "WITH x AS (SELECT id FROM (VALUES (1),(2)) v(id)) "
            "SELECT a.id FROM x a JOIN x b ON a.id=b.id",
            repetitions=1, minimum_baseline_time_ms=0,
        )
        evaluation = next(item for item in plan.evaluations if item.candidate.rule_id == "inline-reused-cte")
        self.assertTrue(evaluation.equivalent)
        self.assertIsNotNone(evaluation.rewritten_time_ms)

    def test_cancellation_interrupts_running_sql_and_rolls_back_index(self):
        import psycopg

        settings = DatabaseSettings()
        control = AnalysisControl()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            connection.execute("CREATE TEMP TABLE pqo_cancel_test (id integer)")
            connection.commit()
            timer = Timer(0.3, control.cancel)
            started = monotonic()
            try:
                timer.start()
                with self.assertRaises(AnalysisCancelled):
                    with control.monitor(connection):
                        try:
                            connection.execute("CREATE INDEX pqo_cancel_trial ON pqo_cancel_test (id)")
                            connection.execute("SELECT pg_sleep(10)")
                        finally:
                            connection.rollback()
            finally:
                timer.cancel()
                timer.join()
            self.assertLess(monotonic() - started, 5)
            self.assertIsNone(connection.execute("SELECT to_regclass('pg_temp.pqo_cancel_trial')").fetchone()[0])
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_advisory_lock_wait_is_bounded(self):
        import psycopg

        settings = DatabaseSettings(statement_timeout_ms=300)
        environment = SequentialIndexEnvironment(settings)
        with psycopg.connect(**settings.connection_kwargs()) as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            ("pqo-sequential-index-experiment",))
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                started = monotonic()
                with self.assertRaises(psycopg.Error) as raised:
                    with environment.episode("SELECT 1", connection=connection):
                        self.fail("Lock should not have been acquired")
                self.assertIn(raised.exception.sqlstate, ("57014", "55P03"))
                self.assertLess(monotonic() - started, 3)
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_timeout_history_preserves_lower_bound(self):
        import psycopg
        from pqo.history import load_analysis_history

        plan = recommend_sequential_indexes(
            "SELECT pg_sleep(0.05)",
            self.PROJECT_ROOT / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            DatabaseSettings(statement_timeout_ms=10),
            repetitions=1, minimum_baseline_time_ms=0, persist=True,
        )
        try:
            record = next(item for item in load_analysis_history(limit=10000)
                          if item.sequential_analysis_id == plan.sequential_analysis_id)
            self.assertTrue(record.baseline_timed_out)
            with psycopg.connect(**DatabaseSettings().connection_kwargs()) as connection:
                actual = connection.execute("SELECT execution_time_ms FROM pqo.query_run WHERE id = %s",
                                            (plan.query_run_id,)).fetchone()[0]
                self.assertIsNone(actual)
        finally:
            with psycopg.connect(**DatabaseSettings().connection_kwargs()) as connection:
                connection.execute("DELETE FROM pqo.query_run WHERE id = %s", (plan.query_run_id,))

    def test_regular_then_deep_analysis_without_calibration(self):
        query = (
            "SELECT flight_id, scheduled_departure FROM aviation.flights "
            "WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure"
        )
        analysis = analyze_query(
            query,
            self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
            self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
            recommendation_threshold_ms=5,
            persist=False,
            calibration_profile_path=None,
            strategy_model_path=(
                self.PROJECT_ROOT / "models/strategy/strategy_selector.joblib"
            ),
        )
        plan = recommend_sequential_indexes(
            query,
            self.PROJECT_ROOT
            / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            max_steps=2,
            repetitions=1,
            minimum_baseline_time_ms=5,
            minimum_absolute_improvement_ms=5,
            minimum_improvement_ratio=0.05,
            persist=False,
        )

        self.assertGreater(analysis.prediction.predicted_time_ms, 0)
        self.assertEqual(analysis.prediction.calibration_factor, 1.0)
        self.assertGreater(plan.baseline_time_ms, 0)

    def test_prediction_callback_arrives_before_recommendations(self):
        from unittest.mock import patch

        received = []

        def delayed_recommendations(*args, **kwargs):
            self.assertEqual(len(received), 1)
            return ()

        with patch("pqo.analysis_service.analyze_query_structure", side_effect=delayed_recommendations):
            result = analyze_query(
                "SELECT 1", self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
                self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
                persist=False, recommendation_threshold_ms=1e9,
                prediction_ready=lambda prediction, latency: received.append((prediction, latency)),
            )
        self.assertEqual(received[0][0], result.prediction)
        self.assertGreaterEqual(result.analysis_latency_ms, received[0][1])

    def test_deep_analysis_returns_plan_when_baseline_hits_statement_timeout(self):
        settings = DatabaseSettings(statement_timeout_ms=10)

        plan = recommend_sequential_indexes(
            "SELECT pg_sleep(0.05)",
            self.PROJECT_ROOT
            / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            settings,
            max_steps=1,
            repetitions=1,
            minimum_baseline_time_ms=0,
            persist=False,
        )

        self.assertTrue(plan.baseline_timed_out)
        self.assertEqual(plan.baseline_time_ms, 10.0)
        self.assertEqual(plan.baseline_samples_ms, ())
        self.assertEqual(plan.terminal_reason, "no_candidates")

    def test_logistics_lateral_query_timeout_does_not_abort_deep_analysis(self):
        query = """
            SELECT s.shipment_id, s.tracking_code, c.carrier_name,
                   origin.facility_name AS origin_name,
                   destination.facility_name AS destination_name,
                   latest_event.event_type, latest_event.event_time,
                   ROUND(EXTRACT(EPOCH FROM (
                       COALESCE(s.delivered_at, latest_event.event_time, now())
                       - s.shipped_at
                   )) / 3600.0, 2) AS hours_in_transit
            FROM logistics.shipments AS s
            JOIN logistics.carriers AS c ON c.carrier_id = s.carrier_id
            JOIN logistics.facilities AS origin
              ON origin.facility_id = s.origin_facility_id
            JOIN logistics.facilities AS destination
              ON destination.facility_id = s.destination_facility_id
            LEFT JOIN LATERAL (
                SELECT te.event_type, te.event_time, te.facility_id
                FROM logistics.tracking_events AS te
                WHERE te.shipment_id = s.shipment_id
                ORDER BY te.event_time DESC
                LIMIT 1
            ) AS latest_event ON true
            WHERE s.shipped_at IS NOT NULL
              AND s.shipment_status IN (
                  'picked_up', 'in_transit', 'delivered', 'exception'
              )
            ORDER BY hours_in_transit DESC NULLS LAST, s.promised_at
            LIMIT 1000
        """
        settings = DatabaseSettings(statement_timeout_ms=100)

        plan = recommend_sequential_indexes(
            query,
            self.PROJECT_ROOT
            / "models/sequential_dqn/sequential_dqn_index_advisor.pt",
            settings,
            max_steps=1,
            repetitions=1,
            minimum_baseline_time_ms=0,
            persist=False,
        )

        self.assertTrue(plan.baseline_timed_out)
        self.assertGreater(plan.candidate_count, 0)

    def test_applies_measures_and_rolls_back_user_approved_index(self):
        query = (
            "SELECT flight_id, scheduled_departure FROM aviation.flights "
            "WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure"
        )
        action = IndexAction.create(
            "aviation", "flights", ("departure_airport", "scheduled_departure")
        )
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "active-plan.json"
            deployment = apply_verified_indexes(
                query, (action,), record, repetitions=1
            )
            try:
                self.assertTrue(record.is_file())
                self.assertGreater(deployment.baseline_time_ms, 0)
                self.assertGreater(deployment.indexed_time_ms, 0)
            finally:
                rollback = rollback_verified_indexes(record, repetitions=1)
            self.assertEqual(rollback.dropped_indexes, deployment.indexes)
            self.assertGreater(rollback.restored_time_ms, 0)
            self.assertFalse(record.exists())

    def test_proves_and_measures_count_to_exists_rewrite(self):
        result = evaluate_sql_rewrites(
            "SELECT c.customer_id FROM retail.customers c "
            "WHERE c.customer_id <= 100 AND "
            "(SELECT COUNT(*) FROM retail.customer_orders o "
            "WHERE o.customer_id = c.customer_id) > 0",
            repetitions=1,
            minimum_baseline_time_ms=0,
            minimum_absolute_improvement_ms=100,
            minimum_improvement_ratio=0.20,
        )

        self.assertIsNotNone(result.recommended)
        self.assertTrue(result.recommended.equivalent)
        self.assertEqual(
            result.recommended.candidate.rule_id,
            "count-positive-to-exists",
        )
        self.assertGreater(result.recommended.improvement_ratio, 0.20)

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
        self.assertIn("target_insert_count", context)
        self.assertIn("target_update_count", context)
        self.assertIn("target_delete_count", context)
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

    def test_sequential_episode_stops_and_rolls_back_all_indexes(self):
        import psycopg

        settings = DatabaseSettings.from_env()
        environment = SequentialIndexEnvironment(
            settings,
            repetitions=1,
            max_steps=2,
            storage_budget_bytes=16 * 1024 * 1024,
        )
        query = (
            "SELECT flight_id FROM aviation.flights "
            "WHERE departure_airport = 'MSQ' AND status = 'Scheduled'"
        )
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            with environment.episode(query, connection=connection) as episode:
                action = next(
                    candidate
                    for candidate in episode.available_actions
                    if candidate.kind.value == "create"
                )
                transition = episode.step(action)
                self.assertTrue(transition.accepted)
                self.assertEqual(transition.next_state.step, 1)
                self.assertGreater(transition.index_size_bytes, 0)
                self.assertTrue(transition.candidate_uses_created_index)
                self.assertTrue(transition.candidate_uses_selected_index)
                live_count = connection.execute(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE schemaname = 'aviation' AND indexname LIKE 'pqo_seq_%'"
                ).fetchone()[0]
                self.assertEqual(live_count, 1)
                stopped = episode.step(IndexAction.stop())
                self.assertTrue(stopped.next_state.done)
                self.assertEqual(stopped.terminal_reason, "stop")
                self.assertEqual(episode.available_actions, ())

            remaining = connection.execute(
                "SELECT count(*) FROM pg_indexes "
                "WHERE schemaname = 'aviation' AND indexname LIKE 'pqo_seq_%'"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_sequential_episode_rejects_index_outside_budget(self):
        environment = SequentialIndexEnvironment(
            repetitions=1,
            max_steps=2,
            storage_budget_bytes=1,
        )
        query = "SELECT flight_id FROM aviation.flights WHERE status = 'Scheduled'"
        with environment.episode(query) as episode:
            action = next(
                candidate
                for candidate in episode.available_actions
                if candidate.kind.value == "create"
            )
            transition = episode.step(action)

            self.assertFalse(transition.accepted)
            self.assertEqual(transition.terminal_reason, "budget_exceeded")
            self.assertEqual(transition.next_state, transition.previous_state)
            self.assertEqual(transition.reward, -0.10)

    def test_combined_analysis_is_persisted(self):
        import psycopg

        result = analyze_query(
            "SELECT flight_id FROM aviation.flights "
            "WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure",
            self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
            self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
            recommendation_threshold_ms=0,
            persist=True,
            strategy_model_path=(
                self.PROJECT_ROOT / "models/strategy/strategy_selector.joblib"
            ),
        )
        self.assertIsNotNone(result.query_run_id)
        self.assertIsNotNone(result.recommendation)
        self.assertIn(
            result.strategy_prediction["strategy"],
            {"NOOP", "CREATE_INDEX", "REWRITE_QUERY"},
        )

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

    def test_structural_feedback_round_trip_is_persisted(self):
        import psycopg

        result = analyze_query(
            "SELECT order_id FROM retail.customer_orders "
            "ORDER BY order_id OFFSET 5000",
            self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
            self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
            recommendation_threshold_ms=1_000_000_000,
            persist=True,
        )
        recommendation = next(
            item
            for item in result.structural_recommendations
            if item.rule_id == "large-offset-pagination"
        )
        self.assertIsNotNone(recommendation.recommendation_id)

        record_recommendation_decision(
            recommendation.recommendation_id,
            RecommendationDecision.ACCEPTED,
            "Проверяем keyset-пагинацию",
        )
        measurement = record_recommendation_measurement(
            recommendation.recommendation_id,
            120.0,
            75.0,
            "Три медианных запуска",
        )

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            stored = connection.execute(
                """
                SELECT status, decision_note, baseline_time_ms,
                       optimized_time_ms, measured_improvement_ratio,
                       measurement_outcome
                FROM pqo.structural_recommendation
                WHERE id = %s
                """,
                (recommendation.recommendation_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM pqo.query_run WHERE id = %s",
                (result.query_run_id,),
            )

        self.assertEqual(stored[0], "accepted")
        self.assertEqual(stored[1], "Проверяем keyset-пагинацию")
        self.assertEqual(stored[2:4], (120.0, 75.0))
        self.assertAlmostEqual(stored[4], 0.375)
        self.assertEqual(stored[5], "improved")
        self.assertEqual(measurement.measurement_outcome.value, "improved")

    def test_automatic_having_validation_is_saved_and_rolled_back(self):
        import psycopg

        sql_text = (
            "SELECT order_status, count(*) FROM retail.customer_orders "
            "GROUP BY order_status HAVING order_status = 'paid'"
        )
        analysis = analyze_query(
            sql_text,
            self.PROJECT_ROOT / "models/xgboost/xgboost_query_time.joblib",
            self.PROJECT_ROOT / "models/dqn/dqn_index_advisor.pt",
            recommendation_threshold_ms=1_000_000_000,
            persist=True,
        )
        recommendation = next(
            item
            for item in analysis.structural_recommendations
            if item.rule_id == "non-aggregate-having-filter"
        )
        record_recommendation_decision(
            recommendation.recommendation_id,
            RecommendationDecision.ACCEPTED,
        )

        validation = validate_and_save_structural_recommendation(
            sql_text, recommendation, repetitions=1
        )

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            stored = connection.execute(
                """
                SELECT validation.equivalent, validation.rolled_back,
                       validation.baseline_time_ms,
                       validation.candidate_time_ms,
                       feedback.measured_at IS NOT NULL
                FROM pqo.structural_validation AS validation
                JOIN pqo.structural_recommendation AS feedback
                  ON feedback.id = validation.recommendation_id
                WHERE validation.id = %s
                """,
                (validation.validation_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM pqo.query_run WHERE id = %s",
                (analysis.query_run_id,),
            )

        self.assertTrue(stored[0])
        self.assertTrue(stored[1])
        self.assertGreater(stored[2], 0)
        self.assertGreater(stored[3], 0)
        self.assertTrue(stored[4])

    def test_materialized_view_trial_leaves_no_schema(self):
        import psycopg

        sql_text = (
            "SELECT a.airport_code, count(*) AS flight_count "
            "FROM aviation.airports a "
            "JOIN aviation.flights f "
            "ON f.departure_airport = a.airport_code "
            "GROUP BY a.airport_code"
        )
        recommendation = StructuralRecommendation(
            RecommendationCategory.MATERIALIZED_VIEW,
            "expensive-summary-materialization",
            RecommendationPriority.MEDIUM,
            "Проверить materialized view",
            "Повторяемая агрегация",
            "Создать пробное представление",
            "Сравнить время",
            "CREATE MATERIALIZED VIEW placeholder AS SELECT 1 WITH NO DATA;",
        )

        validation = validate_structural_recommendation(
            sql_text, recommendation, repetitions=1
        )

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM pg_namespace "
                "WHERE nspname LIKE 'pqo_trial_%'"
            ).fetchone()[0]
        self.assertTrue(validation.supported)
        self.assertTrue(validation.equivalent)
        self.assertTrue(validation.rolled_back)
        self.assertGreater(validation.artifact_creation_time_ms, 0)
        self.assertEqual(remaining, 0)


    def test_read_only_rewrite_batch_metadata_and_control_split(self):
        from dataclasses import replace
        import json
        from pqo.rewrite_batch import BatchCase, collect_batch
        from pqo.rewrite_learning import prepare_dataset
        sql = "SELECT x FROM (VALUES (1), (2), (3)) AS v(x) WHERE x=1 OR x=2 ORDER BY x"
        case = BatchCase("integration-case", "integration-family", "control", "or-equalities", sql)
        settings = replace(DatabaseSettings.from_env(), statement_timeout_ms=1500)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "batch"
            state = collect_batch([case], settings, output)
            self.assertEqual(state["cases"][0]["status"], "completed")
            run = list(iter_journal(output / "journal"))[0]
            self.assertEqual(run["dataset_role"], "control")
            self.assertEqual(run["collection"]["family_id"], "integration-family")
            self.assertTrue(run["candidates"][0]["equivalence"])
            split = json.loads((output / "split.json").read_text(encoding="utf-8"))
            rows, report = prepare_dataset([output / "journal"], split)
            self.assertEqual(len(rows), 1)
            self.assertEqual(report["families"]["control"], 1)
            self.assertFalse(report["training_ready"])


if __name__ == "__main__":
    unittest.main()
