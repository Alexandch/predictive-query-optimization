import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_comparison import TransactionIndexComparison, source_context_digest, sql_digest
from pqo.transaction_holdout import FrozenIndexSelection, _query_key, freeze_index_selection, prepare_holdout, validate_index_holdout
from pqo.transaction_index_trial import LAB_DATABASE, TransactionIndexTrial, TrialReading


ROOT = Path(__file__).resolve().parents[1]


class TransactionHoldoutTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname="source", allowed_schemas=frozenset({"university"}))
        self.actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at"))
            for key in ("student_id", "offering_id"))
        self.development = (ROOT / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        self.control_sql = (ROOT / "examples/transactions/university-index-holdout.sql").read_text(encoding="utf-8")
        self.trial = TransactionIndexTrial(self.actions[0], "lab", "measured",
            tuple(TrialReading(n, (100.,)*3, (10.,)*3, (100.,)*3, True) for n in (1, 2, 3)), (), (), True, True,
            additional_actions=self.actions[1:], frequencies=((1, 1), (2, 1), (3, 1)))
        self.comparison = TransactionIndexComparison("lab", ((1, 10), (2, 1)), (self.trial, self.trial),
            2, True, 0, (), requested_series=2, completed_series=2, coordinates=((1, 1), (2, 1)),
            series_preferences=(1, 1), development_sql_digest=sql_digest(self.development),
            source_context_digest=source_context_digest(self.settings))
        self.selection = freeze_index_selection(self.comparison, 0, self.development, self.settings)

    def test_freeze_preserves_actions_thresholds_and_distinguishes_manual_hypothesis(self):
        self.assertTrue(self.selection.development_stable)
        self.assertEqual(self.selection.actions, self.actions)
        manual = freeze_index_selection(replace(self.comparison, preferred_variant=None), 0, self.development, self.settings)
        self.assertFalse(manual.development_stable)
        one = freeze_index_selection(replace(self.comparison, requested_series=1), 0, self.development, self.settings)
        self.assertFalse(one.development_stable)

    def test_freeze_rejects_stale_partial_failed_rejected_or_unverified_evidence(self):
        for report, row, text, settings in (
            (replace(self.comparison, completed=False), 0, self.development, self.settings),
            (self.comparison, True, self.development, self.settings),
            (self.comparison, 5, self.development, self.settings),
            (self.comparison, 0, self.development + "\n", self.settings),
            (self.comparison, 0, self.development, replace(self.settings, host="another")),
            (replace(self.comparison, trials=(replace(self.trial, accepted_for_reads=False),)), 0, self.development, self.settings),
            (replace(self.comparison, trials=(replace(self.trial, rollback_verified=False),)), 0, self.development, self.settings),
            (replace(self.comparison, development_sql_digest=""), 0, self.development, self.settings),
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                freeze_index_selection(report, row, text, settings)

    def test_new_values_are_accepted_without_mutating_development_or_selecting_other_indexes(self):
        workload = prepare_holdout(self.control_sql, self.selection, self.settings)
        self.assertEqual(len(workload.queries), 3)
        self.assertTrue(workload.sql_text.startswith("BEGIN;"))
        self.assertNotIn("UPDATE", workload.sql_text)
        with patch("pqo.transaction_holdout.run_transaction_index_set_trial", return_value=self.trial) as run:
            report = validate_index_holdout(self.selection, self.control_sql, self.settings, "lab", test_copy_confirmed=True)
        self.assertTrue(report.passed_for_reads)
        self.assertTrue(report.validated_on_both)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[3] == self.actions for call in run.call_args_list))
        self.assertEqual(len({call.kwargs["deadline"] for call in run.call_args_list}), 1)
        self.assertTrue(all("frequencies" not in call.kwargs for call in run.call_args_list))  # control unit weights, not development 10:1

    def test_normalized_reuse_and_duplicate_control_rejected_before_any_execution(self):
        reused = "select enrollment_id,registered_at,status from university.enrollments where student_id=17001 order by registered_at desc; -- comment"
        mixed = self.control_sql + "\n" + reused
        for text in (reused, mixed, self.control_sql + self.control_sql):
            with self.subTest(text=text[:20]), patch("pqo.transaction_holdout.run_transaction_index_set_trial") as run:
                with self.assertRaises(ValueError):
                    validate_index_holdout(self.selection, text, self.settings, "lab", test_copy_confirmed=True)
                run.assert_not_called()

    def test_writes_functions_locks_parameters_context_unsafe_schema_and_large_scripts_refused_offline(self):
        texts = ("BEGIN; SELECT 1; ROLLBACK;", "UPDATE university.enrollments SET status='completed';",
            "WITH x AS (DELETE FROM university.enrollments RETURNING *) SELECT * FROM x;",
            "SELECT * INTO university.x FROM university.enrollments;", "SET work_mem='1GB'; SELECT 1;",
            "SELECT * FROM university.enrollments WHERE student_id=$1;",
            "SELECT * FROM university.enrollments WHERE student_id=19007 FOR UPDATE;",
            "SELECT pg_sleep(1) FROM university.enrollments;", "SELECT * FROM other.enrollments WHERE student_id=19007;",
            "SELECT * FROM enrollments WHERE student_id=19007;", "SELECT * FROM university.enrollments;" * 21,
            " " * 500001, "SELECT '\x00';", "SELECT * FROM university.enrollments WHERE (")
        for text in texts:
            with self.subTest(text=text[:50]), patch("pqo.transaction_holdout.run_transaction_index_set_trial") as run:
                with self.assertRaises(ValueError):
                    validate_index_holdout(self.selection, text, self.settings, "lab", test_copy_confirmed=True)
                run.assert_not_called()

    def test_copy_and_context_mismatch_require_new_explicit_selection(self):
        for settings, target, confirmed in ((self.settings, "lab", False), (self.settings, "source", True),
            (self.settings, "another-lab", True), (replace(self.settings, user="another"), "lab", True)):
            with self.subTest(target=target), patch("pqo.transaction_holdout.run_transaction_index_set_trial") as run:
                with self.assertRaises(ValueError):
                    validate_index_holdout(self.selection, self.control_sql, settings, target, test_copy_confirmed=confirmed)
                run.assert_not_called()

    def test_control_success_cannot_promote_unstable_development(self):
        selection = replace(self.selection, development_stable=False)
        with patch("pqo.transaction_holdout.run_transaction_index_set_trial", return_value=self.trial):
            report = validate_index_holdout(selection, self.control_sql, self.settings, "lab", test_copy_confirmed=True)
        self.assertTrue(report.passed_for_reads)
        self.assertFalse(report.validated_on_both)

    def test_failure_regression_and_cross_series_drift_never_pass_control(self):
        for second in (replace(self.trial, accepted_for_reads=False),
            replace(self.trial, readings=tuple(replace(r, before_ms=(150.,)*3, restored_ms=(150.,)*3) for r in self.trial.readings))):
            with self.subTest(second=second.accepted_for_reads), patch("pqo.transaction_holdout.run_transaction_index_set_trial", side_effect=[self.trial, second]):
                report = validate_index_holdout(self.selection, self.control_sql, self.settings, "lab", test_copy_confirmed=True)
            self.assertTrue(report.completed)
            self.assertFalse(report.passed_for_reads)
            self.assertTrue(report.reasons)

    def test_early_failure_cleanup_budget_and_final_cancellation_preserve_evidence_without_retry(self):
        for mode in ("cleanup", "error", "declined", "cancel", "budget"):
            control = AnalysisControl()
            def run(*args, **kwargs):
                if mode == "cancel":
                    control.cancel()
                return replace(self.trial, rollback_verified=mode != "cleanup",
                    status=mode if mode in {"error", "declined"} else "measured")
            with self.subTest(mode=mode), patch("pqo.transaction_holdout.run_transaction_index_set_trial", side_effect=run) as runner, \
                    patch("pqo.transaction_holdout.time.monotonic", side_effect=[0, 1, 181] if mode == "budget" else [0, 1, 2]):
                report = validate_index_holdout(self.selection, self.control_sql, self.settings, "lab",
                    test_copy_confirmed=True, control=control)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(len(report.trials), 1)
            self.assertFalse(report.completed)
            self.assertFalse(report.passed_for_reads)


@skipUnless(os.getenv("PQO_TRANSACTION_TRIAL_TESTS") == "1", "requires dedicated university index lab")
class TransactionHoldoutPostgresTests(TestCase):
    def test_real_fixed_pair_on_new_values_has_two_readonly_trials_and_verified_rollback(self):
        import psycopg
        from pqo.transaction_index_trial import _read_slice
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))
        development = (ROOT / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        control_sql = (ROOT / "examples/transactions/university-index-holdout.sql").read_text(encoding="utf-8")
        actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at")) for key in ("student_id", "offering_id"))
        def snapshot():
            result = []
            for name in (settings.dbname, LAB_DATABASE):
                with psycopg.connect(**replace(settings, dbname=name).connection_kwargs()) as connection:
                    result.append((connection.execute("SELECT * FROM university.enrollments WHERE student_id IN (17001,19007,19008,19009) ORDER BY enrollment_id").fetchall(),
                        connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall()))
            return result
        before = snapshot()
        # Predetermined manual hypothesis, NOT fabricated development measurements.
        # Real freeze/provenance gates are tested separately; speed is never required.
        queries, _ = _read_slice(development, settings, actions[0])
        selection = FrozenIndexSelection(actions, LAB_DATABASE, 3, False, tuple(_query_key(q) for _, q in queries),
            sql_digest(development), source_context_digest(settings), 1., .05)
        report = validate_index_holdout(selection, control_sql, settings, LAB_DATABASE, test_copy_confirmed=True)
        self.assertTrue(report.completed)
        self.assertEqual(len(report.trials), 2)
        self.assertTrue(all(trial.status == "measured" and trial.rollback_verified and len(trial.used_index_names) == 2 for trial in report.trials))
        self.assertFalse(report.validated_on_both)  # intentionally manual hypothesis in this integration fixture
        self.assertEqual(before, snapshot())
