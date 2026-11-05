import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_comparison import compare_transaction_indexes, comparison_preference, index_variants
from pqo.transaction_index_trial import (
    LAB_DATABASE, TransactionIndexTrial, TrialReading, assess_readings,
    run_transaction_index_set_trial, validate_frequencies,
)
from pqo.transaction_indexes import RelationIndexes


class TransactionComparisonTests(TestCase):
    def setUp(self):
        self.a = IndexAction.create("university", "enrollments", ("student_id",))
        self.b = IndexAction.create("university", "enrollments", ("offering_id",))
        self.c = IndexAction.create("university", "enrollments", ("status",))
        self.settings = replace(DatabaseSettings(), dbname="source", allowed_schemas=frozenset({"university"}))
        self.text = "BEGIN; SELECT * FROM university.enrollments WHERE student_id=1; SELECT * FROM university.enrollments WHERE offering_id=2; UPDATE university.enrollments SET status='completed' WHERE student_id=1; ROLLBACK;"

    def trial(self, actions, indexed=(10., 100.), weights=((1, 1), (2, 1)), **kwargs):
        readings = tuple(TrialReading(n, (100.,)*3, (value,)*3, (100.,)*3, value < 100)
            for n, value in enumerate(indexed, 1))
        return TransactionIndexTrial(actions[0], "lab", kwargs.pop("status", "measured"), readings, (3,), (),
            kwargs.pop("accepted_for_reads", True), kwargs.pop("rollback_verified", True),
            additional_actions=tuple(actions[1:]), frequencies=weights, **kwargs)

    def test_variants_bounded_deduplicated_and_never_triples(self):
        self.assertEqual(len(index_variants((self.a, self.b, self.c))), 6)
        self.assertTrue(all(len(item) <= 2 for item in index_variants((self.a, self.b, self.c))))
        for actions in ((), (self.a, self.a), (self.a, self.b, self.c, IndexAction.noop())):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                index_variants(actions)

    def test_frequencies_must_be_complete_explicit_positive_integer_counts(self):
        self.assertEqual(validate_frequencies((1, 2)), ((1, 1), (2, 1)))
        for values in ({1: 1}, {1: 1, 2: 0}, {1: 1, 2: -1}, {1: 1, 2: 1.5},
                       {1: 1, 2: True}, {1: 1, 2: 10001}, {1: 1, 2: 1, 3: 2}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_frequencies((1, 2), values)

    def test_rare_regression_cannot_be_hidden_by_a_large_frequency_weight(self):
        readings = (TrialReading(1, (100.,)*3, (1.,)*3, (100.,)*3, True),
                    TrialReading(2, (10.,)*3, (20.,)*3, (10.,)*3, False))
        reasons = assess_readings(readings, minimum_gain_ms=1, minimum_gain_ratio=.05, frequencies={1: 10000, 2: 1})
        self.assertTrue(any("SELECT 2" in reason and "регрессия" in reason for reason in reasons))

    def test_explicit_weights_change_preference_without_changing_sample_count(self):
        a = self.trial((self.a,), weights=((1, 100), (2, 1)))
        b = self.trial((self.b,), indexed=(100., 10.), weights=a.frequencies)
        self.assertEqual(comparison_preference((a, b), minimum_gain_ms=5, minimum_gain_ratio=.05)[0], 0)
        a = replace(a, frequencies=((1, 1), (2, 100)))
        b = replace(b, frequencies=a.frequencies)
        self.assertEqual(comparison_preference((a, b), minimum_gain_ms=5, minimum_gain_ratio=.05)[0], 1)

    def test_pair_must_add_value_over_a_single_index(self):
        a = self.trial((self.a,))
        b = self.trial((self.b,), (100., 90.))
        pair = self.trial((self.a, self.b), (10., 99.))
        preferred, _, exclusions = comparison_preference((a, b, pair), minimum_gain_ms=5, minimum_gain_ratio=.05)
        self.assertEqual(preferred, 0)
        self.assertEqual(exclusions[0][0], 2)
        pair = self.trial((self.a, self.b), (10., 10.))
        self.assertEqual(comparison_preference((a, b, pair), minimum_gain_ms=5, minimum_gain_ratio=.05)[0], 2)

    def test_cross_variant_drift_and_close_results_do_not_produce_a_false_winner(self):
        a, b = self.trial((self.a,)), self.trial((self.b,), (11., 100.))
        self.assertIsNone(comparison_preference((a, b), minimum_gain_ms=5, minimum_gain_ratio=.05)[0])
        b = replace(b, readings=(replace(b.readings[0], before_ms=(150.,)*3, restored_ms=(150.,)*3), b.readings[1]))
        preferred, notes, _ = comparison_preference((a, b), minimum_gain_ms=5, minimum_gain_ratio=.05)
        self.assertIsNone(preferred)
        self.assertIn("между вариантами", " ".join(notes))

    def test_bad_input_refused_before_any_connection_or_trial(self):
        for kwargs in ({"frequencies": {1: 1}}, {"test_copy_confirmed": False}, {"copy_database": "SOURCE"}):
            options = {"test_copy_confirmed": True, "copy_database": "lab", **kwargs}
            with self.subTest(options=options), patch("pqo.transaction_comparison.run_transaction_index_set_trial") as trial, self.assertRaises(ValueError):
                compare_transaction_indexes(self.text, self.settings, actions=(self.a, self.b), **options)
            trial.assert_not_called()

    def test_batch_preserves_parameters_and_stops_on_unverified_rollback(self):
        failed = self.trial((self.a,), rollback_verified=False)
        with patch("pqo.transaction_comparison.run_transaction_index_set_trial", return_value=failed) as run:
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a, self.b),
                test_copy_confirmed=True, frequencies={1: 10, 2: 2})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["frequencies"], ((1, 10), (2, 2)))
        self.assertFalse(report.completed)
        self.assertIsNone(report.preferred_variant)
        self.assertEqual(len(report.trials), 1)

    def test_cancel_between_variants_preserves_completed_trial_without_preference(self):
        control = AnalysisControl()
        def first(*args, **kwargs):
            control.cancel()
            return self.trial((self.a,))
        with patch("pqo.transaction_comparison.run_transaction_index_set_trial", side_effect=first) as run:
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a, self.b),
                test_copy_confirmed=True, control=control)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(report.trials), 1)
        self.assertFalse(report.completed)
        self.assertIsNone(report.preferred_variant)

    def test_global_budget_exhaustion_does_not_start_another_trial(self):
        with patch("pqo.transaction_comparison.time.monotonic", side_effect=[0, 181]), \
             patch("pqo.transaction_comparison.run_transaction_index_set_trial") as run:
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a,), test_copy_confirmed=True)
        self.assertFalse(report.trials)
        self.assertFalse(report.completed)
        run.assert_not_called()

    def compare_series(self, side_effect, *, series_count=2, **kwargs):
        with patch("pqo.transaction_comparison.run_transaction_index_set_trial", side_effect=side_effect) as run:
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a, self.b),
                test_copy_confirmed=True, series_count=series_count, **kwargs)
        return report, run

    def test_repeated_series_reverse_order_keep_variant_identity_and_require_same_winner(self):
        def measured(text, settings, target, actions, **kwargs):
            values = (10., 100.) if actions == (self.a,) else (100., 10.) if actions == (self.b,) else (10., 10.)
            return self.trial(actions, values)
        report, run = self.compare_series(measured, series_count=3)
        self.assertEqual([call.args[3] for call in run.call_args_list],
            [(self.a,), (self.b,), (self.a, self.b), (self.a, self.b), (self.b,), (self.a,),
             (self.b,), (self.a, self.b), (self.a,)])
        self.assertEqual(report.series_preferences, (3, 3, 3))
        self.assertEqual(report.preferred_variant, 2)  # first-series row, not last executed row
        self.assertEqual(report.coordinate(3), (2, 3))
        self.assertEqual(report.coordinate(7), (3, 3))
        self.assertTrue(report.completed)
        self.assertTrue(report.stable_for_reads)
        self.assertEqual(report.completed_series, 3)
        self.assertEqual(report.expected_variants, 9)
        self.assertEqual(len({call.kwargs["deadline"] for call in run.call_args_list}), 1)

    def test_winner_change_or_ambiguous_series_blocks_overall_preference(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                first = [self.trial((self.a,), (10., 100.)), self.trial((self.b,), (100., 90.)),
                    self.trial((self.a, self.b), (10., 99.))]  # pair adds too little
                second = [self.trial((self.a, self.b), (99., 10.)), self.trial((self.b,), (100., 10.)),
                    self.trial((self.a,), (10., 100.) if ambiguous else (90., 100.))]
                report, _ = self.compare_series(first + second)
                self.assertEqual(report.series_preferences, (1, None if ambiguous else 2))
                self.assertTrue(report.completed)
                self.assertFalse(report.stable_for_reads)
                self.assertIsNone(report.preferred_variant)
                self.assertIn("серии не выбрали", " ".join(report.reasons))
                self.assertEqual({row for row, _ in report.pair_exclusions}, {2, 3})

    def test_cross_series_baseline_drift_blocks_even_matching_individual_choices(self):
        first = self.trial((self.a,))
        second = replace(first, readings=tuple(replace(item, before_ms=(150.,)*3, restored_ms=(150.,)*3)
            for item in first.readings))
        with patch("pqo.transaction_comparison.run_transaction_index_set_trial", side_effect=[first, second]):
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a,),
                test_copy_confirmed=True, series_count=2)
        self.assertEqual(report.series_preferences, (1, 1))
        self.assertIsNone(report.preferred_variant)
        self.assertFalse(report.stable_for_reads)
        self.assertIn("между сериями", " ".join(report.reasons))

    def test_single_series_never_claims_stability(self):
        with patch("pqo.transaction_comparison.run_transaction_index_set_trial", return_value=self.trial((self.a,))):
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a,), test_copy_confirmed=True)
        self.assertEqual(report.preferred_variant, 0)
        self.assertFalse(report.stable_for_reads)
        self.assertEqual(report.series_preferences, (1,))

    def test_series_count_rejected_before_trials_for_boolean_fraction_or_out_of_range(self):
        for value in (True, 0, 4, 1.5, "2", None):
            with self.subTest(value=value), patch("pqo.transaction_comparison.run_transaction_index_set_trial") as run:
                with self.assertRaises(ValueError):
                    compare_transaction_indexes(self.text, self.settings, "lab", (self.a,),
                        test_copy_confirmed=True, series_count=value)
                run.assert_not_called()

    def test_shared_budget_preserves_first_series_but_never_reports_stability(self):
        with patch("pqo.transaction_comparison.time.monotonic", side_effect=[0, 1, 181]), \
             patch("pqo.transaction_comparison.run_transaction_index_set_trial", return_value=self.trial((self.a,))) as run:
            report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a,),
                test_copy_confirmed=True, series_count=2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report.completed_series, 1)
        self.assertEqual(report.series_preferences, (1,))
        self.assertFalse(report.completed)
        self.assertFalse(report.stable_for_reads)
        self.assertIsNone(report.preferred_variant)

    def test_cancellation_after_last_trial_or_failure_in_second_series_cannot_leave_a_preference(self):
        for mode in ("cancel", "cleanup"):
            with self.subTest(mode=mode):
                control = AnalysisControl()
                count = 0
                def measured(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2 and mode == "cancel":
                        control.cancel()
                    return self.trial((self.a,), rollback_verified=count != 2 or mode != "cleanup")
                with patch("pqo.transaction_comparison.run_transaction_index_set_trial", side_effect=measured) as run:
                    report = compare_transaction_indexes(self.text, self.settings, "lab", (self.a,),
                        test_copy_confirmed=True, series_count=2 if mode == "cancel" else 3, control=control)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(report.completed_series, 1)
                self.assertFalse(report.completed)
                self.assertIsNone(report.preferred_variant)

    def test_two_index_runner_rolls_back_and_verifies_every_attempt_even_if_one_check_fails(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        def execute(statement, *args):
            cursor = MagicMock()
            if isinstance(statement, str):
                if "current_database()" in statement:
                    cursor.fetchone.return_value = ("lab",)
                elif "pg_try_advisory" in statement:
                    cursor.fetchone.return_value = (True,)
                elif "SELECT c.relkind" in statement:
                    cursor.fetchone.return_value = ("r",)
                elif "pg_relation_size" in statement:
                    cursor.fetchone.return_value = (1024,)
            return cursor
        connection.execute.side_effect = execute
        def phase(*args, **kwargs):
            samples = {1: [100.] * 3, 2: [100.] * 3}
            if args[6] == "B с индексами":
                kwargs["index_usage"].update(args[7])
                return {1: [1.] * 3, 2: [1.] * 3}, {1, 2}
            return samples, set()
        with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id", "offering_id"))), \
             patch("pqo.transaction_index_trial._measure_phase", side_effect=phase), \
             patch("pqo.transaction_index_trial._verify_absent", side_effect=[False, True]) as verify:
            report = run_transaction_index_set_trial(self.text, self.settings, "lab", (self.a, self.b),
                test_copy_confirmed=True, frequencies={1: 2, 2: 1})
        self.assertEqual(verify.call_count, 2)
        self.assertFalse(report.rollback_verified)
        self.assertFalse(report.accepted_for_reads)
        self.assertEqual(len(report.index_names), 2)
        self.assertEqual(len(report.used_index_names), 2)
        self.assertEqual(report.index_size_bytes, 2048)
        self.assertEqual(report.weighted_baseline_ms, 300)
        self.assertEqual(report.weighted_indexed_ms, 3)
        self.assertEqual(report.skipped_writes, (3,))
        self.assertTrue(all(call.kwargs == {"force_rollback": True} for call in connection.transaction.call_args_list))

    def test_unused_second_index_and_partial_creation_error_never_accept_a_pair(self):
        import psycopg
        for mode in ("unused", "second-create-error", "total-size-limit"):
            with self.subTest(mode=mode):
                connection = MagicMock()
                connection.__enter__.return_value = connection
                creates = []
                def execute(statement, *args):
                    cursor = MagicMock()
                    if not isinstance(statement, str):
                        creates.append(statement)
                        if mode == "second-create-error" and len(creates) == 2:
                            raise psycopg.errors.QueryCanceled("second creation timeout")
                    elif "current_database()" in statement:
                        cursor.fetchone.return_value = ("lab",)
                    elif "pg_try_advisory" in statement:
                        cursor.fetchone.return_value = (True,)
                    elif "SELECT c.relkind" in statement:
                        cursor.fetchone.return_value = ("r",)
                    elif "pg_relation_size" in statement:
                        cursor.fetchone.return_value = (40 * 1024 * 1024 if mode == "total-size-limit" else 1024,)
                    return cursor
                connection.execute.side_effect = execute
                def phase(*args, **kwargs):
                    if args[6] == "B с индексами":
                        kwargs["index_usage"].add(args[7][0])
                        return {1: [1.] * 3, 2: [1.] * 3}, {1, 2}
                    return {1: [100.] * 3, 2: [100.] * 3}, set()
                with patch("pqo.transaction_index_trial.psycopg.connect", return_value=connection), \
                     patch("pqo.transaction_index_trial._load_relation_indexes", return_value=RelationIndexes("university", "enrollments", ("student_id", "offering_id"))), \
                     patch("pqo.transaction_index_trial._measure_phase", side_effect=phase), \
                     patch("pqo.transaction_index_trial._verify_absent", return_value=True) as verify:
                    report = run_transaction_index_set_trial(self.text, self.settings, "lab", (self.a, self.b), test_copy_confirmed=True)
                self.assertFalse(report.accepted_for_reads)
                self.assertTrue(report.rollback_verified)
                self.assertEqual(verify.call_count, 2)
                self.assertEqual(len(creates), 2)
                self.assertEqual(report.status, "measured" if mode == "unused" else "error")


@skipUnless(os.getenv("PQO_TRANSACTION_TRIAL_TESTS") == "1", "requires dedicated university index lab")
class TransactionComparisonPostgresTests(TestCase):
    def settings(self):
        return replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen", allowed_schemas=frozenset({"university"}))

    def snapshot(self, settings):
        import psycopg
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            return (connection.execute("SELECT * FROM university.enrollments WHERE student_id=17001 ORDER BY enrollment_id").fetchall(),
                connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall())

    def test_real_repeated_comparison_preserves_rows_catalogs_and_stable_variant_numbers(self):
        settings = self.settings()
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        before = [self.snapshot(replace(settings, dbname=name)) for name in (settings.dbname, LAB_DATABASE)]
        actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at")) for key in ("student_id", "offering_id"))
        report = compare_transaction_indexes(text, settings, LAB_DATABASE, actions,
            test_copy_confirmed=True, frequencies={1: 10, 2: 1}, series_count=2)
        self.assertTrue(report.completed)
        self.assertEqual(report.completed_series, 2)
        self.assertEqual(report.coordinates, ((1, 1), (1, 2), (1, 3), (2, 3), (2, 2), (2, 1)))
        self.assertTrue(all(trial.status == "measured" and trial.rollback_verified for trial in report.trials))
        self.assertTrue(all(len(trial.used_index_names) == 2 for trial in report.trials if len(trial.actions) == 2))
        if report.preferred_variant is not None:
            self.assertEqual(len(set(report.series_preferences)), 1)
            self.assertTrue(report.stable_for_reads)
        self.assertEqual(before, [self.snapshot(replace(settings, dbname=name)) for name in (settings.dbname, LAB_DATABASE)])

    def test_real_two_index_variant_uses_both_and_keeps_source_and_copy_unchanged(self):
        settings = self.settings()
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        before = [self.snapshot(replace(settings, dbname=name)) for name in (settings.dbname, LAB_DATABASE)]
        actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at")) for key in ("student_id", "offering_id"))
        report = run_transaction_index_set_trial(text, settings, LAB_DATABASE, actions, test_copy_confirmed=True, frequencies={1: 10, 2: 1})
        self.assertEqual(report.status, "measured")
        self.assertTrue(report.rollback_verified)
        self.assertEqual(len(report.used_index_names), 2)
        self.assertEqual(report.frequencies, ((1, 10), (2, 1)))
        self.assertEqual(report.skipped_writes, (3,))
        self.assertEqual(before, [self.snapshot(replace(settings, dbname=name)) for name in (settings.dbname, LAB_DATABASE)])

    def test_real_cancellation_with_two_created_indexes_verifies_both_absent(self):
        settings = self.settings()
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-index-comparison.sql").read_text(encoding="utf-8")
        before = self.snapshot(replace(settings, dbname=LAB_DATABASE))
        control = AnalysisControl(progress=lambda message: control.cancel() if message.startswith("B с индексами") else None)
        actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at")) for key in ("student_id", "offering_id"))
        report = run_transaction_index_set_trial(text, settings, LAB_DATABASE, actions, test_copy_confirmed=True, control=control)
        self.assertEqual(report.status, "cancelled")
        self.assertTrue(report.rollback_verified)
        self.assertFalse(report.accepted_for_reads)
        self.assertEqual(len(report.index_names), 2)
        self.assertGreater(report.index_size_bytes, 0)
        self.assertEqual(before, self.snapshot(replace(settings, dbname=LAB_DATABASE)))
