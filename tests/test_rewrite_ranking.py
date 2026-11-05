import copy
import math
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pqo.rewrite_batch import Relationship
from pqo.rewrite_ranking import ranking_cases, summarize_queries, _pick, replay_ranking, frozen_holdout
from pqo.sql_rewrite import generate_sql_rewrites


class RewriteRankingTests(unittest.TestCase):
    def test_queries_have_several_different_candidates(self):
        relation = Relationship("retail", "order_items", "order_id", "retail", "customer_orders", "order_id")
        cases = ranking_cases([relation])
        self.assertEqual(len(cases), 3)
        self.assertEqual([len(generate_sql_rewrites(case.sql, max_combined_candidates=0, include_window_materialization=False)) for case in cases], [2, 3, 3])
        self.assertTrue(all(case.role == "control" for case in cases))
        self.assertTrue(all(case.family_id == relation.family_id for case in cases))

    def test_score_threshold_allows_noop(self):
        self.assertIsNone(_pick([-.1, -.2], 0))
        self.assertEqual(_pick([.1, .5], 0), 1)
        self.assertIsNone(_pick([.001, .01], math.log(1.05)))

    def queries(self):
        return [{"family_id": str(index), "candidates": [{"label": -1., "accepted": False},
                                                          {"label": 1., "accepted": True}],
                 "choices": {"model": 1, "generator_first": 0, "estimated_cost": 0, "noop": None}}
                for index in range(10)]

    def test_best_selection_passes_only_with_independent_families(self):
        result = summarize_queries(self.queries())
        self.assertTrue(result["passes_ranking_gate"])
        self.assertEqual(result["policies"]["model"]["mean_log_regret"], 0)
        self.assertEqual(result["policies"]["generator_first"]["mean_log_regret"], 1)
        self.assertFalse(result["automatic_activation"])
        self.assertFalse(summarize_queries(self.queries()[:9])["passes_ranking_gate"])

    def test_tie_with_cost_baseline_does_not_pass_gate(self):
        queries = self.queries()
        for row in queries:
            row["choices"]["estimated_cost"] = 1
        self.assertFalse(summarize_queries(queries)["passes_ranking_gate"])

    def test_slow_selected_variant_is_rejected_not_applied(self):
        queries = self.queries()
        for row in queries:
            row["choices"]["model"] = 0
        result = summarize_queries(queries)
        self.assertEqual(result["policies"]["model"]["mean_realized_log_gain"], 0)
        self.assertEqual(result["policies"]["model"]["mean_log_regret"], 1)
        self.assertFalse(result["passes_ranking_gate"])

    def test_repeating_one_family_does_not_inflate_quality(self):
        queries = self.queries()
        queries[0]["choices"]["model"] = None
        original = summarize_queries(queries)
        repeated = summarize_queries(queries + [copy.deepcopy(queries[0]) for _ in range(50)])
        self.assertAlmostEqual(original["policies"]["model"]["mean_log_regret"],
                               repeated["policies"]["model"]["mean_log_regret"])

    def test_empty_ranking_set_never_passes(self):
        self.assertFalse(summarize_queries([])["passes_ranking_gate"])

    def replay_fixture(self):
        from test_rewrite_learning import record
        run = record()
        run["dataset_role"] = "control"
        run["collection"] = {"family_id": "control-family"}
        run["candidates"][0]["accepted"] = True
        second = record("b", variant="SELECT c FROM retail.t", time=20)["candidates"][0]
        second["accepted"] = False
        run["candidates"].append(second)
        artifact = {"feature_version": 1,
                    "model": SimpleNamespace(predict=Mock(return_value=[.1, .9])),
                    "vectorizer": SimpleNamespace(transform=Mock(side_effect=lambda value: value))}
        return run, artifact

    def replay(self, run, artifact):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "run-test.json").write_text(json.dumps(run), encoding="utf-8")
            return replay_ranking(artifact, directory)

    def test_replay_scores_without_training_and_rejects_slow_choice(self):
        run, artifact = self.replay_fixture()
        result = self.replay(run, artifact)
        self.assertEqual(result["metrics"]["queries"], 1)
        self.assertEqual(result["queries"][0]["choices"]["model"], 1)
        self.assertEqual(result["metrics"]["policies"]["model"]["mean_realized_log_gain"], 0)
        self.assertGreater(result["metrics"]["policies"]["generator_first"]["mean_realized_log_gain"], 0)
        artifact["model"].predict.assert_called_once()

    def test_incomplete_query_is_excluded_not_given_a_partial_oracle(self):
        run, artifact = self.replay_fixture()
        run["candidates"][1]["status"] = "error"
        result = self.replay(run, artifact)
        self.assertEqual(result["metrics"]["queries"], 0)
        self.assertEqual(result["metrics"]["excluded"]["incomplete_or_invalid_query"], 1)
        artifact["model"].predict.assert_not_called()

    def test_forged_acceptance_cannot_make_a_slow_query_applicable(self):
        run, artifact = self.replay_fixture()
        run["candidates"][1]["accepted"] = True
        result = self.replay(run, artifact)
        self.assertEqual(result["metrics"]["queries"], 0)

    def test_training_or_test_data_is_not_ranked_as_control(self):
        run, artifact = self.replay_fixture()
        run["dataset_role"] = "development"
        result = self.replay(run, artifact)
        self.assertEqual(result["metrics"]["queries"], 0)
        self.assertEqual(result["metrics"]["excluded"]["not_control_data"], 1)

    def test_frozen_artifact_requires_matching_dataset_digest(self):
        artifact = {"feature_version": 1, "provenance": [], "dataset_report": {"dataset_sha256": "expected"}}
        with patch("pqo.rewrite_ranking.prepare_dataset", return_value=([], {"dataset_sha256": "different"})):
            with self.assertRaisesRegex(ValueError, "does not match"):
                frozen_holdout(artifact, "unused")

    def test_frozen_artifact_rejects_training_relationship_leakage(self):
        artifact = {"feature_version": 1, "provenance": [], "dataset_report": {"dataset_sha256": "same"}}
        rows = [{"role": "development", "run_ids": ["a"]}, {"role": "control", "run_ids": ["b"]}]
        records = [{"run_id": key, "original_sql": "SELECT 1", "candidates": [],
                    "collection": {"family_id": "overlap"}} for key in ("a", "b")]
        with patch("pqo.rewrite_ranking.prepare_dataset", return_value=(rows, {"dataset_sha256": "same"})), \
                patch("pqo.rewrite_ranking.iter_journal", return_value=iter(records)):
            with self.assertRaisesRegex(ValueError, "leakage"):
                frozen_holdout(artifact, "unused")
