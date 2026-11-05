import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_indexes import ExistingIndex, RelationIndexes, build_transaction_index_report, collect_transaction_index_candidates
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport, collect_transaction_plans
from pqo.transaction_write_pressure import WriteAssumption, assess_write_pressure


class TransactionWritePressureTests(TestCase):
    def setUp(self):
        self.relation = RelationIndexes("university", "enrollments",
            ("enrollment_id", "student_id", "offering_id", "status", "registered_at"),
            (ExistingIndex("pk", ("enrollment_id",)),), write_metadata_known=True, relation_kind="r")

    def fixture(self, body=None, relation=None):
        body = body or """SELECT enrollment_id,status FROM university.enrollments WHERE student_id=1;
            UPDATE university.enrollments SET status='completed' WHERE student_id=1;
            INSERT INTO university.enrollments(enrollment_id,student_id,status) VALUES (2,1,'active');
            DELETE FROM university.enrollments WHERE student_id=2"""
        text = "BEGIN; " + body + "; ROLLBACK;"
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, "source", tuple(StatementPlan(item.number, "planned", "fixture", [{"Plan": {
            "Node Type": "ModifyTable", "Operation": item.kind.capitalize(), "Plan Rows": 0,
            "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 25}]}}]) for item in inspection.statements))
        relation = relation or self.relation
        indices = build_transaction_index_report(inspection, plans, {("university", "enrollments"): relation})
        plain = next(item.action for item in indices.candidates if item.action.key_columns == ("student_id",) and not item.action.include_columns)
        covering = next(item.action for item in indices.candidates if item.action.key_columns == ("student_id",) and "status" in item.action.include_columns)
        return text, plans, indices, plain, covering

    def test_explicit_row_scenario_scales_with_index_count_not_plan_rows(self):
        text, plans, indices, plain, covering = self.fixture()
        assumptions = (WriteAssumption(2, 4, 200), WriteAssumption(3, 10, 1), WriteAssumption(4, 2, 30))
        one = assess_write_pressure(text, plans, indices, (plain,), assumptions=assumptions, budget=1000)
        pair = assess_write_pressure(text, plans, indices, (plain, covering), assumptions=assumptions, budget=1000)
        self.assertEqual(one.known_exposure, 870)
        self.assertEqual(pair.known_exposure, 1740)
        self.assertEqual(one.budget_state, "within_scenario")
        self.assertEqual(pair.budget_state, "exceeded")
        self.assertEqual(pair.rows[0].plan_input_rows, 25)
        self.assertEqual(pair.rows[0].rows_per_call, 200)
        self.assertIsNone(pair.net_benefit_ms)

    def test_unknown_rows_are_not_zero_and_explicit_zero_is_respected(self):
        text, plans, indices, plain, covering = self.fixture()
        unknown = assess_write_pressure(text, plans, indices, (plain,), budget=0)
        self.assertEqual(unknown.unknown_commands, (2, 3, 4))
        self.assertEqual(unknown.budget_state, "unknown")
        zero = assess_write_pressure(text, plans, indices, (plain,), assumptions=tuple(WriteAssumption(n, 1, 0) for n in (2, 3, 4)), budget=0)
        self.assertFalse(zero.unknown_commands)
        self.assertEqual(zero.budget_state, "within_scenario")
        partial = assess_write_pressure(text, plans, indices, (plain,), assumptions=(WriteAssumption(2, 1, 10), WriteAssumption(3), WriteAssumption(4)), budget=5)
        self.assertEqual(partial.budget_state, "exceeded")  # known portion already exceeds even with unknown remainder

    def test_include_changes_create_new_hot_obstacle_but_other_updates_are_not_called_free(self):
        text, plans, indices, plain, covering = self.fixture()
        one = assess_write_pressure(text, plans, indices, (plain,))
        pair = assess_write_pressure(text, plans, indices, (plain, covering))
        self.assertEqual(one.rows[0].hot_status, "no_direct_overlap")
        self.assertEqual(pair.rows[0].include_columns_in_set, ("status",))
        self.assertEqual(pair.rows[0].incremental_hot_columns, ("status",))
        self.assertEqual(pair.rows[0].hot_status, "new_columns_in_set")
        self.assertIn("всем обычным индексам", " ".join(one.rows[0].limitations))
        # A syntactic SET x=x is still not treated as a measured value change.
        text = text.replace("status = 'completed'", "status = status")
        text, plans, indices, plain, covering = self.fixture("SELECT enrollment_id,status FROM university.enrollments WHERE student_id=1; UPDATE university.enrollments SET status=status WHERE student_id=1")
        result = assess_write_pressure(text, plans, indices, (covering,))
        self.assertIn("не доказательство изменения значения", " ".join(result.rows[0].limitations))

    def test_existing_hot_columns_and_historical_statistics_do_not_predict_future_hot(self):
        relation = replace(self.relation, indexes=self.relation.indexes + (ExistingIndex("status_idx", ("status",)),), updates=100, hot_updates=60)
        text, plans, indices, plain, covering = self.fixture(relation=relation)
        report = assess_write_pressure(text, plans, indices, (covering,))
        self.assertEqual(report.rows[0].incremental_hot_columns, ())
        self.assertEqual(report.rows[0].hot_status, "existing_columns_in_set")
        self.assertEqual(report.rows[0].historical_hot_percent, 60)
        self.assertIsNone(report.rows[0].exposure)
        self.assertIsNone(report.net_benefit_ms)

    def test_unsafe_or_missing_dependency_metadata_stays_unknown_not_hot_safe(self):
        variants = (replace(self.relation, write_metadata_known=False), replace(self.relation, generated_columns=("status",)),
            replace(self.relation, indexes=(ExistingIndex("expr", (None,), compatible=False),)),
            replace(self.relation, has_user_triggers=True), replace(self.relation, has_rules=True),
            replace(self.relation, has_descendants=True), replace(self.relation, relation_kind="p"))
        for relation in variants:
            with self.subTest(relation=relation):
                text, plans, indices, plain, covering = self.fixture(relation=relation)
                report = assess_write_pressure(text, plans, indices, (covering,))
                self.assertEqual(report.rows[0].hot_status, "unknown")
                self.assertIsNone(report.rows[0].incremental_hot_columns)

    def test_triggers_rules_partitions_and_cascades_cannot_get_complete_budget_approval(self):
        for relation in (replace(self.relation, has_user_triggers=True), replace(self.relation, has_rules=True),
            replace(self.relation, has_descendants=True), replace(self.relation, fk_update_writes=True), replace(self.relation, fk_delete_writes=True)):
            with self.subTest(relation=relation):
                text, plans, indices, plain, covering = self.fixture(relation=relation)
                result = assess_write_pressure(text, plans, indices, (plain,),
                    assumptions=tuple(WriteAssumption(n, 1, 1) for n in (2, 3, 4)), budget=100)
                self.assertFalse(result.scope_complete)
                self.assertEqual(result.budget_state, "unknown")
        text, plans, indices, plain, covering = self.fixture()
        plans = replace(plans, statements=tuple(replace(plan, status="error") if plan.number == 2 else plan for plan in plans.statements))
        result = assess_write_pressure(text, plans, indices, (plain,),
            assumptions=tuple(WriteAssumption(n, 1, 1) for n in (2, 3, 4)), budget=100)
        self.assertFalse(result.scope_complete)
        self.assertEqual(result.budget_state, "unknown")

    def test_modifying_cte_upsert_and_unknown_function_refuse_complete_model(self):
        select = "SELECT enrollment_id,status FROM university.enrollments WHERE student_id=1; "
        for dml in ("WITH x AS (DELETE FROM university.enrollments WHERE student_id=1 RETURNING *) SELECT * FROM x",
            "INSERT INTO university.enrollments(enrollment_id,student_id) VALUES (1,1) ON CONFLICT(enrollment_id) DO UPDATE SET status='x'",
            "UPDATE university.enrollments SET status=university.side_effect() WHERE student_id=1"):
            with self.subTest(dml=dml):
                text, plans, indices, plain, covering = self.fixture(select + dml)
                result = assess_write_pressure(text, plans, indices, (covering,), assumptions=(WriteAssumption(2, 1, 10),), budget=1000)
                self.assertFalse(result.scope_complete)
                self.assertEqual(result.budget_state, "unknown")
                if "side_effect" not in dml:
                    self.assertEqual(result.unknown_commands, (2,))

    def test_bad_assumptions_and_stale_inputs_refused_without_connection(self):
        text, plans, indices, plain, covering = self.fixture()
        for assumptions in ((WriteAssumption(2),), (WriteAssumption(2, True), WriteAssumption(3), WriteAssumption(4)),
            (WriteAssumption(2, 1, -1), WriteAssumption(3), WriteAssumption(4)),
            (WriteAssumption(2, 1, 1.5), WriteAssumption(3), WriteAssumption(4)),
            (WriteAssumption(2, 1, True), WriteAssumption(3), WriteAssumption(4))):
            with self.subTest(assumptions=assumptions), patch("psycopg.connect") as connect, self.assertRaises(ValueError):
                assess_write_pressure(text, plans, indices, (plain,), assumptions=assumptions)
            connect.assert_not_called()
        for kwargs in ({"budget": True}, {"budget": -1}, {"budget": 1.5}):
            with self.assertRaises(ValueError):
                assess_write_pressure(text, plans, indices, (plain,), **kwargs)
        with self.assertRaises(ValueError):
            assess_write_pressure(text.replace("BEGIN", "BEGIN ISOLATION LEVEL SERIALIZABLE"), plans, indices, (plain,))

    def test_cancel_and_empty_dml_set_do_not_claim_transaction_readonly(self):
        text, plans, indices, plain, covering = self.fixture()
        control = AnalysisControl()
        control.cancel()
        with patch("psycopg.connect") as connect, self.assertRaises(AnalysisCancelled):
            assess_write_pressure(text, plans, indices, (plain,), control=control)
        connect.assert_not_called()
        text, plans, indices, plain, covering = self.fixture("SELECT enrollment_id,status FROM university.enrollments WHERE student_id=1")
        result = assess_write_pressure(text, plans, indices, (plain,), budget=0)
        self.assertFalse(result.rows)
        self.assertIn("внешняя нагрузка", " ".join(result.notes))

    def test_read_evidence_must_match_entire_index_set_and_select_slice(self):
        from pqo.transaction_index_trial import TransactionIndexTrial, TrialReading
        text, plans, indices, plain, covering = self.fixture()
        trial = TransactionIndexTrial(plain, "copy", "measured", (TrialReading(1, (10.,)*3, (1.,)*3, (10.,)*3, True),), (2, 3, 4), (), True, True)
        result = assess_write_pressure(text, plans, indices, (plain,), read_trial=trial)
        self.assertEqual(result.read_trial.weighted_gain_ms, 9)
        self.assertIsNone(result.net_benefit_ms)
        with self.assertRaises(ValueError):
            assess_write_pressure(text, plans, indices, (plain, covering), read_trial=trial)


@skipUnless(os.getenv("PQO_UNIVERSITY_TESTS") == "1", "requires isolated university database")
class TransactionWritePressurePostgresTests(TestCase):
    def test_real_catalog_write_flags_and_scenario_never_execute_dml_or_create_indexes(self):
        import psycopg
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        def snapshot():
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                return (connection.execute("SELECT * FROM university.enrollments WHERE student_id=17001 ORDER BY enrollment_id").fetchall(),
                    connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall())
        before = snapshot()
        plans = collect_transaction_plans(text, settings)
        indices = collect_transaction_index_candidates(text, settings, plans=plans)
        candidate = next(item for item in indices.candidates if item.action.key_columns == ("student_id",) and "status" in item.action.include_columns)
        self.assertTrue(candidate.relation.write_metadata_known)
        self.assertEqual(candidate.relation.relation_kind, "r")
        with patch("psycopg.connect") as connect:
            result = assess_write_pressure(text, plans, indices, (candidate.action,), assumptions=(WriteAssumption(3, 10, 100), WriteAssumption(4, 50, 1), WriteAssumption(5, 2, 100)), budget=1000)
        connect.assert_not_called()
        self.assertEqual(result.known_exposure, 1250)
        self.assertEqual(result.budget_state, "exceeded")
        self.assertEqual(result.rows[0].include_columns_in_set, ("status",))
        self.assertEqual(before, snapshot())
