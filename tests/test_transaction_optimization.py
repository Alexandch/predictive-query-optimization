import os
from dataclasses import replace
import time
from unittest import TestCase, skipUnless
from unittest.mock import patch

import psycopg
from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_optimization import (
    OptimizationVariant,
    optimization_request_digest,
    run_transaction_optimization,
    select_best_variant,
)
from pqo.transaction_mixed_recommendation import MixedReplayPolicy, assess_mixed_replay
from pqo.transaction_replay import result_digest
from pqo.transaction_select_rewrite import generate_transaction_select_rewrites
from test_transaction_select_rewrite import TEXT


class OptimizationTests(TestCase):
    def setUp(self):
        self.settings = replace(
            DatabaseSettings(),
            dbname="source",
            allowed_schemas=frozenset({"university"}),
        )
        self.action = IndexAction.create("university", "items", ("value",), ("id",))
        self.variant = next(
            x.variant_sql
            for x in generate_transaction_select_rewrites(TEXT, self.settings)
            if x.rule_id == "subquery-order-without-limit"
        )

    def run_trial(self, **kwargs):
        return run_transaction_optimization(
            TEXT, self.settings, "lab", 2, self.variant, (self.action,), **kwargs
        )

    def test_invalid_or_unconfirmed_requests_never_connect(self):
        for kwargs in (
            {},
            {"test_copy_confirmed": True},
            {
                "test_copy_confirmed": True,
                "disposable_databases_confirmed": True,
                "series": True,
            },
            {
                "test_copy_confirmed": True,
                "disposable_databases_confirmed": True,
                "policy": "invalid",
            },
        ):
            with patch("psycopg.connect") as connect, self.assertRaises(ValueError):
                self.run_trial(**kwargs)
            connect.assert_not_called()
        for actions in (
            (),
            (self.action, self.action),
            (IndexAction.create("university", "other", ("id",)),),
        ):
            with patch("psycopg.connect") as connect, self.assertRaises(ValueError):
                run_transaction_optimization(
                    TEXT,
                    self.settings,
                    "lab",
                    2,
                    self.variant,
                    actions,
                    test_copy_confirmed=True,
                    disposable_databases_confirmed=True,
                )
            connect.assert_not_called()

    def test_request_identity_binds_all_inputs_without_password(self):
        values = (
            TEXT,
            self.settings,
            "lab",
            2,
            self.variant,
            (self.action,),
            2,
            MixedReplayPolicy(),
        )
        digest = optimization_request_digest(*values)
        for index, value in (
            (0, TEXT + " -- different"),
            (1, replace(self.settings, dbname="other")),
            (2, "lab2"),
            (4, self.variant.replace("SUM(q.id)", "MAX(q.id)")),
            (5, (IndexAction.create("university", "items", ("id",)),)),
            (6, 1),
            (7, MixedReplayPolicy(minimum_gain_ms=7)),
        ):
            changed = list(values)
            changed[index] = value
            self.assertNotEqual(digest, optimization_request_digest(*changed))
        changed = list(values)
        changed[1] = replace(self.settings, password="only-in-memory")
        self.assertEqual(digest, optimization_request_digest(*changed))

    def measured(self, kind, total):
        from test_transaction_mixed_recommendation import MixedRecommendationTests

        report = MixedRecommendationTests().fixture()
        samples = []
        for sample in report.samples:
            if sample.phase == "B":
                commands = (
                    replace(sample.commands[0], client_ms=total - 20),
                    sample.commands[1],
                )
                sample = replace(sample, transaction_client_ms=total, commands=commands)
            if kind == "rewrite":
                sample = replace(
                    sample,
                    index_size_bytes=0,
                    index_final_size_bytes=0,
                    index_build_ms=0,
                )
            samples.append(sample)
        report = replace(
            report,
            samples=tuple(samples),
            actions=() if kind == "rewrite" else report.actions,
        )
        report = replace(
            report,
            recommendation=assess_mixed_replay(
                report,
                report.policy,
                expected_request_digest=report.request_digest,
                mode=kind,
            ),
        )
        return OptimizationVariant(kind, report)

    def test_combined_is_measured_and_may_lose_to_a_simpler_variant(self):
        variants = (
            self.measured("indexes", 30),
            self.measured("rewrite", 50),
            self.measured("combined", 65),
        )
        decision, best, eligible, _ = select_best_variant(
            variants, True, MixedReplayPolicy()
        )
        self.assertEqual((decision, best), ("recommended", "indexes"))
        self.assertEqual(len(eligible), 3)

    def test_near_ties_do_not_create_a_unique_winner(self):
        variants = (
            self.measured("indexes", 30),
            self.measured("rewrite", 32),
            self.measured("combined", 31),
        )
        decision, best, eligible, _ = select_best_variant(
            variants, True, MixedReplayPolicy()
        )
        self.assertEqual((decision, best), ("multiple", None))
        self.assertEqual(len(eligible), 3)
        self.assertEqual(
            select_best_variant(variants, False, MixedReplayPolicy())[0], "inconclusive"
        )
        ties = tuple(
            self.measured(kind, 50) for kind in ("indexes", "rewrite", "combined")
        )
        self.assertEqual(
            select_best_variant(
                ties, True, MixedReplayPolicy(minimum_gain_ms=0, minimum_gain_ratio=0)
            )[0],
            "multiple",
        )

    def test_a_joint_plan_cannot_mask_incorrect_rewriting_or_write_regression(self):
        variants = [
            self.measured("indexes", 50),
            self.measured("rewrite", 45),
            self.measured("combined", 25),
        ]
        bad = replace(
            variants[1].measurement, correctness_verified=False, results_equal=False
        )
        bad = replace(
            bad,
            recommendation=assess_mixed_replay(
                bad,
                bad.policy,
                expected_request_digest=bad.request_digest,
                mode="rewrite",
            ),
        )
        variants[1] = OptimizationVariant("rewrite", bad)
        _, best, eligible, _ = select_best_variant(
            tuple(variants), True, MixedReplayPolicy()
        )
        self.assertEqual(best, "indexes")
        self.assertNotIn("combined", eligible)
        report = variants[2].measurement
        samples = tuple(
            replace(
                s,
                commands=(s.commands[0], replace(s.commands[1], client_ms=25)),
                transaction_client_ms=50,
            )
            if s.phase == "B"
            else s
            for s in report.samples
        )
        report = replace(report, samples=samples)
        self.assertFalse(
            assess_mixed_replay(
                report,
                report.policy,
                expected_request_digest=report.request_digest,
                mode="combined",
            ).recommended
        )


@skipUnless(
    os.getenv("PQO_DISPOSABLE_REPLAY_TESTS") == "1",
    "Explicit owned PostgreSQL fixture opt-in required",
)
class OptimizationPostgresTests(TestCase):
    def setUp(self):
        OptimizationTests.setUp(self)
        from test_transaction_replay import DisposableReplayPostgresTests

        fixture = DisposableReplayPostgresTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.reference = fixture.reference

    def trial(self, text=TEXT, variant=None, **kwargs):
        return run_transaction_optimization(
            text,
            self.settings,
            self.reference,
            2,
            variant or self.variant,
            (self.action,),
            series=2,
            test_copy_confirmed=True,
            disposable_databases_confirmed=True,
            **kwargs,
        )

    def absent(self, report):
        with psycopg.connect(
            **replace(self.settings, dbname="postgres").connection_kwargs()
        ) as c:
            self.assertFalse(
                c.execute(
                    "SELECT datname FROM pg_database WHERE datname=ANY(%s)",
                    (list(report.created_databases),),
                ).fetchall()
            )

    def test_same_frozen_seed_and_independent_four_way_execution(self):
        report = self.trial()
        self.assertEqual(report.status, "completed", report.reasons)
        self.assertTrue(report.comparable and report.cleanup_verified)
        self.assertEqual(len(report.observations), 14)
        self.assertEqual(len(report.created_databases), 15)
        groups = {}
        for i in range(1, 5001):
            value = 700 if i == 1 else i % 100
            count, total = groups.get(value, (0, 0))
            groups[value] = count + 1, total + i
        aggregate = result_digest([(key, *groups[key]) for key in sorted(groups)], True)
        for item in report.variants:
            self.assertTrue(item.measurement.correctness_verified)
            self.assertEqual(len(item.measurement.samples), 6)
            for sample in item.measurement.samples:
                self.assertEqual(
                    sample.commands[0].result_sha256, result_digest([(1, 700)], False)
                )
                self.assertEqual(sample.commands[1].result_sha256, aggregate)
                self.assertEqual(
                    sample.commands[2].result_sha256, result_digest([(1, 700)], True)
                )
                self.assertEqual(
                    bool(sample.index_size_bytes),
                    sample.phase == "B" and item.kind != "rewrite",
                )
        self.absent(report)
        with psycopg.connect(
            **replace(self.settings, dbname=self.reference).connection_kwargs()
        ) as c:
            self.assertEqual(
                c.execute("SELECT value FROM university.items WHERE id=1").fetchone()[
                    0
                ],
                1,
            )

    def test_wrong_replacement_is_rejected_with_and_without_indexes(self):
        wrong = self.variant.replace("GROUP BY", "WHERE q.value<>700 GROUP BY", 1)
        report = self.trial(variant=wrong)
        self.assertTrue(report.comparable and report.cleanup_verified)
        for item in report.variants:
            self.assertEqual(
                item.measurement.correctness_verified, item.kind == "indexes"
            )
            if item.kind != "indexes":
                self.assertFalse(item.measurement.recommendation.recommended)
        self.assertNotIn("combined", report.eligible_variants)
        self.absent(report)

    def test_cancel_after_write_prevents_further_variants_and_cleans_all_copies(self):
        for trigger in (
            "Последовательное выполнение: 2",
            "Серия 2: Только переписывание",
        ):
            with self.subTest(trigger=trigger):
                control = AnalysisControl()
                control.progress = lambda message: (
                    control.cancel() if message.startswith(trigger) else None
                )
                report = self.trial(control=control)
                self.assertEqual(report.status, "cancelled")
                self.assertTrue(report.cleanup_verified)
                self.assertIsNone(report.best_variant)
                self.assertFalse(report.eligible_variants)
                self.assertFalse(
                    any(
                        v.measurement.recommendation.recommended
                        for v in report.variants
                    )
                )
                self.absent(report)

    def test_sql_error_after_write_cleans_the_entire_matrix(self):
        text = TEXT.replace(
            "SELECT id,value FROM university.items WHERE id=1 ORDER BY id;",
            "INSERT INTO university.items(id,value) VALUES(1,99); SELECT id,value FROM university.items WHERE id=1 ORDER BY id;",
        )
        report = self.trial(text=text)
        self.assertEqual(report.status, "error")
        self.assertTrue(report.cleanup_verified)
        self.assertIsNone(report.best_variant)
        self.assertFalse(report.eligible_variants)
        self.assertTrue(any("23505" in message for message in report.reasons))
        self.absent(report)

    def test_isolation_savepoint_and_nontransactional_sequence_state_are_preserved(
        self,
    ):
        with psycopg.connect(
            **replace(self.settings, dbname=self.reference).connection_kwargs()
        ) as c:
            c.execute(
                "CREATE TABLE university.events(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,value integer,label text)"
            )
            c.execute("INSERT INTO university.events(value,label) VALUES(5,'seed')")
        text = """BEGIN ISOLATION LEVEL REPEATABLE READ;
INSERT INTO university.events(value,label) VALUES(7,'kept') RETURNING id;
SAVEPOINT s; INSERT INTO university.events(value,label) VALUES(99,'gone') RETURNING id;
ROLLBACK TO s; RELEASE s;
SELECT id,value FROM university.events WHERE value=7 OR value=8 ORDER BY id; COMMIT;"""
        variant = next(
            x.variant_sql
            for x in generate_transaction_select_rewrites(text, self.settings)
            if x.rule_id == "or-equality-to-in"
        )
        action = IndexAction.create("university", "events", ("value",), ("id",))
        report = run_transaction_optimization(
            text,
            self.settings,
            self.reference,
            6,
            variant,
            (action,),
            test_copy_confirmed=True,
            disposable_databases_confirmed=True,
            series=2,
        )
        self.assertTrue(report.comparable and report.cleanup_verified, report.reasons)
        for observation in report.observations:
            sample = observation.sample
            self.assertEqual(
                sample.transaction_options.isolation_level, "REPEATABLE READ"
            )
            self.assertEqual(
                sample.commands[-1].result_sha256, result_digest([(2, 7)], True)
            )
            self.assertNotEqual(sample.initial_sequences, sample.final_sequences)
        self.assertTrue(
            all(v.measurement.correctness_verified for v in report.variants)
        )
        self.absent(report)
