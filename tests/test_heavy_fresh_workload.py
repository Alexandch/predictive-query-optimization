import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlglot import exp, parse_one

from pqo.heavy_fresh_workload import heavy_fresh_workload
from pqo.paired_workload import paired_workload
from pqo.fresh_measurements import collect_record, records_frame


class HeavyFreshWorkloadTests(unittest.TestCase):
    def test_peer_keys_and_distinct_projection_remain_valid(self):
        for _, case in heavy_fresh_workload():
            tree = parse_one(case.sql_text, read="postgres")
            if "acceptance_peer_rollup" in case.template_id:
                window = next(window for window in tree.find_all(exp.Window)
                              if window.args.get("spec") and window.args["spec"].args.get("kind") == "GROUPS")
                self.assertEqual([item.this.name for item in window.args["order"].expressions],
                                 ["value", "entity", "person"])
            if "validation_distinct_stream" in case.template_id:
                self.assertNotIn("entity", {column.name for window in tree.find_all(exp.Window)
                                            for column in window.find_all(exp.Column)})

    def test_collector_uses_estimated_inputs_and_separate_samples(self):
        plan = SimpleNamespace(features=SimpleNamespace(
            estimated_plan_rows=10,
            as_dict=lambda: {"estimated_total_cost": 7, "actual_total_time_ms": None, "shared_hit_blocks": 0}))
        measurement = SimpleNamespace(samples_ms=(1.,2.,3.), median_time_ms=2.,
            last_result=SimpleNamespace(plan_json=[{"Plan": {"Actual Total Time": 3.}}]))
        with patch("pqo.fresh_measurements.collect_explain", return_value=plan) as explain, \
             patch("pqo.fresh_measurements.collect_database_features", return_value=SimpleNamespace(as_dict=lambda: {})), \
             patch("pqo.fresh_measurements.measure_query_execution", return_value=measurement):
            record = collect_record({"id": 0,"family":"test","split":"train","sql":"SELECT 1"}, None)
        self.assertFalse(explain.call_args.kwargs["analyze"])
        self.assertEqual(record["samples_ms"], [1.,2.,3.])
        self.assertNotIn("actual_total_time_ms", record["features"])
        self.assertNotIn("shared_hit_blocks", record["features"])

    def test_fixed_family_disjoint_workload(self):
        cases = list(heavy_fresh_workload())
        self.assertEqual(len(cases), 84)
        groups = {}
        sqls = set()
        for split, case in cases:
            groups.setdefault(split, set()).add(case.template_id)
            sqls.add(parse_one(case.sql_text, read="postgres").sql(dialect="postgres"))
        self.assertEqual(len(sqls), 84)
        self.assertEqual({key: len(value) for key, value in groups.items()},
                         {"train": 8, "validation": 4, "acceptance": 8})
        self.assertFalse(groups["train"] & (groups["validation"] | groups["acceptance"]))
        self.assertFalse(groups["validation"] & groups["acceptance"])
        previous = {parse_one(case.sql_text, read="postgres").sql(dialect="postgres")
                    for bounds in ((2300,13000,43000), (4100,17000,51000))
                    for case in paired_workload(bounds)}
        self.assertFalse(sqls & previous)

    def test_collector_frame_does_not_invent_or_drop_failed_runs(self):
        record = {"status": "ok", "family": "test", "sql": "SELECT 1", "split": "train",
                  "features": {"estimated_total_cost": 1.}, "samples_ms": [1., 2., 3.]}
        frame = records_frame([record])
        self.assertEqual(frame.actual_total_time_ms.tolist(), [1., 2., 3.])
        for records in ([], [{**record, "status": "error"}], [{**record, "samples_ms": [1., 2.]}]):
            with self.assertRaises(ValueError):
                records_frame(records)
