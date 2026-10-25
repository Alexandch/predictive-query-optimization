import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pqo.rewrite_batch import Relationship, BatchCase
from pqo.rewrite_journal import GENERATOR_VERSION
from pqo.rewrite_pairwise import (PARAMETERS, split_families, final_cases,
                                  candidate_features, fit_pairwise, evaluate, main, load_groups)
from pqo.sql_rewrite import generate_sql_rewrites


class RewritePairwiseTests(unittest.TestCase):
    def test_split_is_stable_disjoint_and_reserves_validation(self):
        development, final = [f"d{i}" for i in range(50)], [f"f{i}" for i in range(19)]
        first = split_families(development, final)
        self.assertEqual(first, split_families(list(reversed(development)), final))
        self.assertEqual({key: len(value) for key, value in first.items()}, {"train": 40, "validation": 10, "final": 19})
        self.assertFalse(set(first["train"]) & set(first["validation"]))
        with self.assertRaises(ValueError):
            split_families(development, [development[0]])

    def test_new_final_forms_have_several_candidates_including_cte_window(self):
        relation = Relationship("retail", "order_items", "order_id", "retail", "customer_orders", "order_id")
        cases = final_cases([relation])
        self.assertEqual([len(generate_sql_rewrites(case.sql)) for case in cases], [2, 3, 4])
        self.assertIn("DENSE_RANK", cases[2].sql)
        self.assertTrue(all(case.role == "control" for case in cases))

    def test_features_have_relative_deltas_and_no_actual_runtime(self):
        plan = lambda cost: [{"Plan": {"Node Type": "Result", "Total Cost": cost, "Plan Rows": 1,
                                      "Actual Total Time": 9999, "Shared Read Blocks": 888}}]
        features = candidate_features("SELECT 1", "SELECT 1 WHERE TRUE", "manual",
                                      {"baseline": plan(100), "variant": plan(10)})
        self.assertLess(features["delta.plan.estimated_total_cost"], 0)
        self.assertFalse(any("actual" in key or "blocks" in key for key in features))
        noop = candidate_features("SELECT 1", "SELECT 1", "NOOP", {"baseline": plan(100), "variant": plan(100)})
        self.assertEqual(noop["candidate.is_noop"], 1)
        self.assertEqual(noop["delta.plan.estimated_total_cost"], 0)

    def groups(self, prefix, count):
        return [{"family_id": f"{prefix}{i}", "run_id": f"run-{prefix}{i}",
                 "features": [{"candidate.is_noop": 1, "benefit": 0},
                              {"candidate.is_noop": 0, "benefit": 0},
                              {"candidate.is_noop": 0, "benefit": 10}],
                 "relevance": [0, 0, 1], "cost_scores": [0, 0],
                 "candidates": [{"rule_id": "a", "accepted": False, "label": -1},
                                {"rule_id": "b", "accepted": True, "label": 1}]} for i in range(count)]

    def test_fit_grouped_ranker_and_keep_validation_out_of_vectorizer(self):
        train, validation = self.groups("train", 30), self.groups("validation", 10)
        validation[0]["features"][0]["validation_only"] = 123
        artifact, result = fit_pairwise(train, validation)
        self.assertEqual(artifact["parameters"]["objective"], "rank:pairwise")
        self.assertNotIn("validation_only", artifact["vectorizer"].get_feature_names_out())
        self.assertFalse(artifact["automatic_activation"])
        self.assertEqual(result["metrics"]["queries"], 10)
        self.assertEqual(len(artifact["training_run_ids"]), 30)

    def test_fit_rejects_family_leakage(self):
        with self.assertRaises(ValueError):
            fit_pairwise(self.groups("x", 30), self.groups("x", 10))

    def test_ties_prefer_noop_not_an_unverified_rewrite(self):
        from types import SimpleNamespace
        artifact = {"model": SimpleNamespace(predict=lambda data: [1, 1, 1]),
                    "vectorizer": SimpleNamespace(transform=lambda data: data)}
        result = evaluate(artifact, self.groups("control", 10))
        self.assertTrue(all(row["choices"]["model"] is None for row in result["queries"]))

    def test_failed_validation_blocks_final_before_loading_model_or_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps({"parameters": PARAMETERS, "schema_version": 1,
                                                       "generator_version": GENERATOR_VERSION}))
            (root / "validation-report.json").write_text(json.dumps({"final_measurements_permitted": False}))
            with patch("sys.argv", ["pairwise", "--phase", "final", "--output", directory]), \
                    patch("pqo.rewrite_pairwise.collect_batch", side_effect=AssertionError("no SQL")), \
                    patch("joblib.load", side_effect=AssertionError("no model loading")), \
                    patch("psycopg.connect", side_effect=AssertionError("no connection")):
                self.assertEqual(main(), 2)
            self.assertFalse((root / "final").exists())

    def measured_fixture(self):
        from test_rewrite_learning import record
        sql = ("SELECT p.id, (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id) AS n "
               "FROM retail.customer_orders p WHERE p.id>=1 AND p.id<=8 ORDER BY p.id")
        case = BatchCase("case-a", "family-a", "development", "rank-scalar-range", sql)
        run = record(original=sql)
        run["dataset_role"] = "development"
        run["collection"] = {"family_id": case.family_id}
        run["candidates"] = []
        for candidate in generate_sql_rewrites(sql):
            entry = record()["candidates"][0]
            entry.update(rule_id=candidate.rule_id, variant_sql=candidate.sql_text, accepted=True)
            run["candidates"].append(entry)
        return case, run

    def load_fixture(self, case, run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journal").mkdir()
            (root / "journal/run-a.json").write_text(json.dumps(run), encoding="utf-8")
            (root / "batch.json").write_text(json.dumps({"status": "completed", "cases": [
                {"case_id": case.case_id, "run_id": run["run_id"], "status": "completed"}]}))
            return load_groups(root, [case])

    def test_loader_adds_noop_and_keeps_one_query_group(self):
        case, run = self.measured_fixture()
        groups, audit = self.load_fixture(case, run)
        self.assertEqual(audit["queries"], 1)
        self.assertFalse(audit["excluded"])
        self.assertEqual(len(groups[0]["features"]), 3)
        self.assertEqual(groups[0]["features"][0]["rule_id"], "NOOP")
        self.assertEqual(groups[0]["relevance"], [0, 1, 1])

    def test_loader_excludes_whole_query_if_a_candidate_measurement_failed(self):
        case, run = self.measured_fixture()
        run["candidates"][1]["status"] = "error"
        groups, audit = self.load_fixture(case, run)
        self.assertEqual(groups, [])
        self.assertTrue(audit["excluded"])

    def test_loader_rejects_changed_query_or_candidate_list(self):
        for field in ("original", "candidate", "role"):
            case, run = self.measured_fixture()
            if field == "original":
                run["original_sql"] = "SELECT 1"
            elif field == "candidate":
                run["candidates"][0]["variant_sql"] = "SELECT 1"
            else:
                run["dataset_role"] = "control"
            groups, audit = self.load_fixture(case, run)
            self.assertFalse(groups)
            self.assertTrue(audit["excluded"])
