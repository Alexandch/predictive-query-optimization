import unittest
from pathlib import Path
from sqlglot import parse_one
from pqo.complex_workload import complex_workload
from pqo.sql_features import extract_sql_features


class ComplexWorkloadTests(unittest.TestCase):
    def test_split_is_family_disjoint_and_sql_is_parseable(self):
        workload = list(complex_workload())
        self.assertEqual(len(workload), 60)
        self.assertEqual(len({case.sql_text for _, case in workload}), 60)
        families = {}
        for split, case in workload:
            families.setdefault(split, set()).add(case.template_id)
            features = extract_sql_features(case.sql_text)
            self.assertGreater(features.cte_count, 0)
            self.assertGreater(features.window_function_count, 0)
            self.assertNotIn("logistics.", case.sql_text)
            self.assertNotIn("chbenchmark.", case.sql_text)
        self.assertEqual({key: len(value) for key, value in families.items()},
                         {"train": 8, "validation": 2, "acceptance": 2})
        self.assertFalse(families["train"] & families["validation"])
        self.assertFalse(families["train"] & families["acceptance"])

    def test_user_diagnostic_is_not_in_any_workload_split(self):
        path = Path(__file__).resolve().parents[1] / "examples/retail-category-share-control.sql"
        def normalized(sql):
            return parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)
        control = normalized(path.read_text(encoding="utf-8"))
        self.assertNotIn(control, {normalized(case.sql_text) for _, case in complex_workload()})
