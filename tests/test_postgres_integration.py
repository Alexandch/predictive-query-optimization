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

    def test_combined_rewrites_are_checked_and_measured_with_duplicates_and_nulls(self):
        import psycopg
        from contextlib import nullcontext
        from unittest.mock import patch

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            connection.execute("CREATE TEMP TABLE pqo_combine_parent(id int)")
            connection.execute("CREATE TEMP TABLE pqo_combine_child(parent_id int)")
            connection.execute("INSERT INTO pqo_combine_parent VALUES (1),(1),(2),(3),(NULL)")
            connection.execute("INSERT INTO pqo_combine_child VALUES (1),(1),(2),(NULL)")
            connection.commit()  # Only connection-local TEMP data; no user tables changed.
            sql = (
                "SELECT q.id FROM (SELECT p.id FROM pqo_combine_parent p "
                "WHERE (SELECT COUNT(*) FROM pqo_combine_child c WHERE c.parent_id=p.id)>0 "
                "ORDER BY p.id DESC) q ORDER BY q.id LIMIT 20"
            )
            with patch("psycopg.connect", side_effect=lambda **kwargs: nullcontext(connection)):
                plan = evaluate_sql_rewrites(sql, settings, repetitions=1,
                    minimum_baseline_time_ms=0, minimum_absolute_improvement_ms=1000000)
            combined = [item for item in plan.evaluations
                        if item.candidate.rule_id.startswith("combined:")]
            self.assertEqual(len(combined), 1)
            self.assertTrue(combined[0].equivalent)
            self.assertIsNotNone(combined[0].rewritten_time_ms)
            self.assertFalse(combined[0].accepted)  # Equivalence alone never enables apply.
            self.assertEqual(combined[0].rejection_reason, "improvement_too_small")
            self.assertEqual(connection.execute(sql).fetchall(), [(1,), (1,), (2,)])

    def test_candidate_evidence_history_roundtrip_and_atomic_rollback(self):
        import psycopg
        from contextlib import nullcontext
        from unittest.mock import patch
        from pqo.repository import save_sequential_analysis
        from pqo.history import load_analysis_history
        from pqo.sequential_recommendation import SequentialCandidateCheck, SequentialRecommendationPlan

        settings = DatabaseSettings.from_env()
        action = IndexAction.create("public", "evidence_history_test", ("id",))
        plan = SequentialRecommendationPlan(
            steps=(), baseline_time_ms=12, final_time_ms=12,
            storage_budget_bytes=10000, used_budget_bytes=0, candidate_count=2,
            decision_threshold=.1, minimum_baseline_time_ms=5,
            minimum_absolute_improvement_ms=1, terminal_reason="statement_timeout",
            candidate_checks=(SequentialCandidateCheck(
                action, 1, .9, False, "statement_timeout", 12,
            ),),
        )
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            with connection.transaction(force_rollback=True):
                # Exercise the real owned-connection migration, twice, but leave
                # both schema and evidence changes inside this rollback-only test.
                with patch("psycopg.connect", side_effect=lambda **kwargs: nullcontext(connection)):
                    run_id, analysis_id = save_sequential_analysis("SELECT 1", plan, settings)
                    save_sequential_analysis("SELECT 1", plan, settings, query_run_id=run_id)
                    record = next(row for row in load_analysis_history(settings, limit=10000)
                                  if row.sequential_analysis_id == analysis_id)
                self.assertEqual(record.candidate_trial_evidence["version"], 1)
                check = record.candidate_trial_evidence["checks"][0]
                self.assertFalse(check["accepted"])
                self.assertIsNone(check["after_time_ms"])
                self.assertEqual(check["rejection_reason"], "statement_timeout")
                self.assertIsNone(connection.execute(
                    "SELECT to_regclass('public.evidence_history_test')"
                ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT id FROM pqo.query_run WHERE id=%s", (run_id,)
            ).fetchone())

    def test_candidate_savepoint_restores_real_ddl_budget_and_recovers_timeout(self):
        import psycopg
        from unittest.mock import patch
        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            connection.execute("CREATE TEMP TABLE pqo_retry_case(a int,b int) ON COMMIT DROP")
            schema = connection.execute("SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()").fetchone()[0]
            environment = SequentialIndexEnvironment(settings, allowed_schemas=frozenset({schema}),
                max_steps=2, repetitions=1, warmup_runs=0)
            query = f'SELECT a,b FROM "{schema}".pqo_retry_case WHERE a=1'
            with environment.episode(query, connection=connection) as episode:
                plain = IndexAction.create(schema, "pqo_retry_case", ("a",))
                cover = IndexAction.create(schema, "pqo_retry_case", ("a",), ("b",))
                with episode.candidate_trial() as trial:
                    episode.step(plain)
                    trial.accept()
                accepted = episode.state
                accepted_names = list(episode._created_index_names)
                with episode.candidate_trial():
                    episode.step(cover)
                    self.assertEqual(connection.execute("SELECT count(*) FROM pg_index WHERE indrelid='pqo_retry_case'::regclass").fetchone()[0], 2)
                self.assertEqual(episode.state, accepted)
                self.assertEqual(episode._created_index_names, accepted_names)
                self.assertEqual(connection.execute("SELECT count(*) FROM pg_index WHERE indrelid='pqo_retry_case'::regclass").fetchone()[0], 1)
                def timed_out_measure(*args, **kwargs):
                    connection.execute("SET LOCAL statement_timeout='5ms'")
                    connection.execute("SELECT pg_sleep(0.05)")
                with patch.object(environment, "_measure", side_effect=timed_out_measure):
                    with self.assertRaises(psycopg.errors.QueryCanceled):
                        with episode.candidate_trial():
                            episode.step(cover)
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
                self.assertEqual(episode.state, accepted)
                self.assertEqual(connection.execute("SELECT count(*) FROM pg_index WHERE indrelid='pqo_retry_case'::regclass").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT to_regclass('pqo_retry_case')").fetchone()[0])

    def test_index_overlap_uses_full_payload_and_usable_index_metadata(self):
        import psycopg
        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            try:
                connection.execute("CREATE TEMP TABLE pqo_overlap_case(a int,b text,c int) ON COMMIT DROP")
                schema = connection.execute("SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()").fetchone()[0]
                connection.execute("CREATE INDEX pqo_overlap_plain ON pqo_overlap_case(a)")
                plain = IndexAction.create(schema, "pqo_overlap_case", ("a",))
                covering = IndexAction.create(schema, "pqo_overlap_case", ("a",), ("b",))
                self.assertTrue(collect_action_database_context(plain, connection=connection)["exact_index_exists"])
                first = collect_action_database_context(covering, connection=connection)
                self.assertFalse(first["prefix_index_exists"])
                self.assertEqual(first["key_overlap_index_count"], 1)
                connection.execute("CREATE INDEX pqo_overlap_partial ON pqo_overlap_case(a) INCLUDE(b) WHERE c>0")
                connection.execute("CREATE INDEX pqo_overlap_expr ON pqo_overlap_case(lower(b),a)")
                connection.execute("CREATE INDEX pqo_overlap_hash ON pqo_overlap_case USING hash(a)")
                connection.execute("CREATE INDEX pqo_overlap_pattern ON pqo_overlap_case(b text_pattern_ops)")
                connection.execute('CREATE INDEX pqo_overlap_collate ON pqo_overlap_case(b COLLATE "C")')
                context = collect_action_database_context(covering, connection=connection)
                self.assertEqual(context["target_index_count"], 6)
                self.assertFalse(context["prefix_index_exists"])
                self.assertEqual(context["key_overlap_index_count"], 1)
                self.assertFalse(collect_action_database_context(
                    IndexAction.create(schema, "pqo_overlap_case", ("b",)), connection=connection)["prefix_index_exists"])
                connection.execute("CREATE INDEX pqo_overlap_cover ON pqo_overlap_case(a) INCLUDE(b)")
                context = collect_action_database_context(covering, connection=connection)
                self.assertTrue(context["exact_index_exists"])
                self.assertTrue(context["prefix_index_exists"])
                self.assertEqual(context["key_overlap_index_count"], 2)
            finally:
                connection.rollback()

    def test_distinct_join_chain_preserves_cross_child_dependencies(self):
        import psycopg
        from pqo.sql_rewrite import generate_sql_rewrites, _queries_are_equivalent

        prefix = """WITH parents(id,label) AS (VALUES
            (1,'same'),(1,'same'),(2,'same'),(3,'other'),(4,'same'),(NULL,'null')),
            first_child(owner_id,group_id) AS (VALUES
            (1,10),(1,10),(1,11),(2,20),(3,NULL),(4,40),(NULL,10)),
            second_child(group_id,flag) AS (VALUES
            (10,'yes'),(10,'yes'),(11,'no'),(20,'no'),(NULL,'yes'),(40,'yes'))
            """
        with psycopg.connect(**DatabaseSettings.from_env().connection_kwargs()) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                for projection, predicate, expected in (
                    ("p.id", "b.flag='yes'", [(1,), (4,)]),
                    ("p.label", "b.flag='yes'", [('same',)]),
                    ("p.id", "p.id=2 OR b.flag='yes'", [(1,), (2,), (4,)]),
                    ("p.id", "b.flag IS NULL", []),
                ):
                    for extra_join in ("", "JOIN parents cp ON cp.id=p.id",
                                       "JOIN first_child a2 ON a2.group_id=b.group_id AND a2.owner_id=p.id"):
                        sql = f"""{prefix} SELECT DISTINCT {projection} FROM parents p
                            JOIN first_child a ON a.owner_id=p.id
                            JOIN second_child b ON b.group_id=a.group_id
                            {extra_join} WHERE {predicate} ORDER BY {projection}"""
                        candidate = next(item for item in generate_sql_rewrites(sql)
                                         if item.rule_id == "distinct-inner-join-to-exists")
                        self.assertTrue(_queries_are_equivalent(connection, sql, candidate.sql_text))
                        self.assertEqual(connection.execute(candidate.sql_text).fetchall(), expected)

    def test_composite_scalar_distinct_preserves_null_keys_and_duplicate_parents(self):
        import psycopg
        from pqo.sql_rewrite import generate_sql_rewrites, _queries_are_equivalent

        sql = """WITH parents(id,kind) AS (VALUES
            (1,'a'),(1,'a'),(1,'b'),(2,'a'),(3,'a'),(NULL,'a'),(1,NULL)),
            children(id,kind,amount) AS (VALUES
            (1,'a',10),(1,'a',10),(1,'a',NULL),(1,'b',4),(2,'a',NULL),(NULL,'a',99),(1,NULL,88))
            SELECT p.id,p.kind,
            (SELECT COUNT(DISTINCT "C".amount) FROM children "C" WHERE "C".id=p.id AND "C".kind=p.kind) AS unique_values,
            (SELECT SUM(s.amount) FROM children s WHERE s.kind=p.kind AND s.id=p.id) AS total,
            (SELECT COUNT(*) FROM children n WHERE p.id=n.id AND p.kind=n.kind) AS n,
            (SELECT AVG(a.amount) FROM children a WHERE a.id=p.id AND a.kind=p.kind) AS average
            FROM parents p"""
        with psycopg.connect(**DatabaseSettings.from_env().connection_kwargs()) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                candidates = [item for item in generate_sql_rewrites(sql) if item.rule_id in {
                    "scalar-aggregate-to-grouped-join", "shared-scalar-aggregates-to-grouped-join"}]
                self.assertEqual(len(candidates), 2)
                for candidate in candidates:
                    self.assertTrue(_queries_are_equivalent(connection, sql, candidate.sql_text))
                    rows = connection.execute(candidate.sql_text).fetchall()
                    self.assertEqual(len(rows), 7)
                    self.assertEqual(sum(row[:2] == (1, 'a') for row in rows), 2)
                    for row in rows:
                        if row[0] is None or row[1] is None or row[0] == 3:
                            self.assertEqual(row[2:], (0, None, 0, None))
                        elif row[:2] == (2, 'a'):
                            self.assertEqual(row[2:], (0, None, 1, None))
                        elif row[:2] == (1, 'a'):
                            self.assertEqual(row[2:], (1, 20, 3, 10))

    def test_shared_scalar_aggregates_preserve_nulls_duplicates_and_empty_inputs(self):
        import psycopg
        from pqo.sql_rewrite import generate_sql_rewrites, _queries_are_equivalent

        with psycopg.connect(**DatabaseSettings.from_env().connection_kwargs()) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                for parents, children in (
                    ("(1),(1),(2),(3),(NULL)", "(1,10),(1,10),(1,NULL),(2,NULL),(NULL,99)"),
                    ("(1),(2),(NULL)", "(NULL::int,NULL::int)"),
                ):
                    sql = f'''WITH parents(id) AS (VALUES {parents}),
                        children(order_id,amount) AS (VALUES {children})
                        SELECT p.id,
                        (SELECT SUM("Child".amount) FROM children "Child" WHERE "Child".order_id=p.id) AS total,
                        (SELECT COUNT(*) FROM children x WHERE x.order_id=p.id) AS n,
                        (SELECT COUNT(y.amount) FROM children y WHERE y.order_id=p.id) AS valued,
                        (SELECT AVG(z.amount) FROM children z WHERE z.order_id=p.id) AS average
                        FROM parents p'''
                    candidate = next(item for item in generate_sql_rewrites(sql)
                                     if item.rule_id == "shared-scalar-aggregates-to-grouped-join")
                    self.assertTrue(_queries_are_equivalent(connection, sql, candidate.sql_text))
                    rows = connection.execute(candidate.sql_text).fetchall()
                    empty = next(row for row in rows if row[0] is None)
                    self.assertEqual(empty, (None, None, 0, 0, None))
                    self.assertEqual(len(rows), len(connection.execute(sql).fetchall()))


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


    def test_grouped_scalar_rewrite_preserves_nulls_counts_and_duplicate_parents(self):
        fixture = ("WITH parents(id) AS (VALUES (1), (1), (2), (3), (NULL::integer)), "
                   "children(owner, amount) AS (VALUES (1,10::numeric), (1,NULL::numeric), "
                   "(2,NULL::numeric), (NULL::integer,99::numeric)) ")
        from pqo.sql_rewrite import generate_sql_rewrites
        for aggregate in ("COUNT(*)", "COUNT(c.amount)", "SUM(c.amount)", "AVG(c.amount)", "MIN(c.amount)", "MAX(c.amount)"):
            sql = (fixture + f"SELECT p.id, (SELECT {aggregate} FROM children c WHERE c.owner=p.id) AS value "
                   "FROM parents p ORDER BY p.id")
            candidates = tuple(item for item in generate_sql_rewrites(sql)
                               if item.rule_id == "scalar-aggregate-to-grouped-join")
            self.assertEqual(len(candidates), 1)
            result = evaluate_sql_rewrites(sql, repetitions=1, minimum_baseline_time_ms=0, _candidates=candidates)
            self.assertEqual(len(result.evaluations), 1)
            self.assertTrue(result.evaluations[0].equivalent, (aggregate, result))
            self.assertIsNotNone(result.evaluations[0].rewritten_time_ms)

    def test_distinct_exists_rewrite_preserves_duplicate_and_null_semantics(self):
        from pqo.sql_rewrite import generate_sql_rewrites
        sql = ("WITH parents(id) AS (VALUES (1), (1), (2), (3), (NULL::integer)), "
               "children(owner, amount) AS (VALUES (1,10), (1,20), (2,NULL::integer), (NULL::integer,99)) "
               "SELECT DISTINCT p.id FROM parents p JOIN children c ON c.owner=p.id "
               "WHERE c.amount>5 OR c.amount IS NULL ORDER BY p.id")
        candidates = tuple(item for item in generate_sql_rewrites(sql)
                           if item.rule_id == "distinct-inner-join-to-exists")
        self.assertEqual(len(candidates), 1)
        result = evaluate_sql_rewrites(sql, repetitions=1, minimum_baseline_time_ms=0, _candidates=candidates)
        self.assertTrue(result.evaluations[0].equivalent)
        self.assertIsNotNone(result.evaluations[0].rewritten_time_ms)

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
