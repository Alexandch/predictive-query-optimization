import importlib.util
from pathlib import Path
import unittest

from sqlglot import parse_one
from pqo.flow_workload import flow_workload
from pqo.structural_workload import structural_workload
from pqo.broad_workload import broad_workload
from pqo.complex_workload import complex_workload
from pqo.explain import _assert_read_only_query


class FlowWorkloadTests(unittest.TestCase):
    def test_disjoint_fixed_reports_and_no_previous_sql(self):
        cases = list(flow_workload())
        self.assertEqual(len(cases), 36)
        normalize = lambda text: parse_one(text, read="postgres").sql(dialect="postgres")
        sqls = {normalize(_assert_read_only_query(case.sql_text)) for _, case in cases}
        self.assertEqual(len(sqls), 36)
        prior = {normalize(case.sql_text) for gen in (structural_workload, broad_workload, complex_workload)
                 for _, case in gen()}
        self.assertFalse(sqls & prior)
        groups = {}
        for split, case in cases:
            groups.setdefault(split, set()).add(case.template_id)
        self.assertEqual({k: len(v) for k, v in groups.items()},
                         {"train": 4, "validation": 4, "acceptance": 4})
        self.assertFalse(groups["train"] & (groups["validation"] | groups["acceptance"]))
        self.assertFalse(groups["validation"] & groups["acceptance"])

    def test_backfill_rejects_changed_historical_plans(self):
        spec = importlib.util.spec_from_file_location(
            "backfill", Path(__file__).resolve().parents[1] / "scripts/backfill_plan_flows.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        row = {name: 100 for name in module.MATCH_FIELDS}
        row["root_node_type"] = "Sort"
        self.assertTrue(module.matches_historical_plan(row, dict(row)))
        self.assertFalse(module.matches_historical_plan(row, {**row, "estimated_total_cost": 200}))
        self.assertFalse(module.matches_historical_plan(row, {**row, "root_node_type": "Limit"}))
