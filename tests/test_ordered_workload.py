import unittest
from sqlglot import parse_one, exp
from pqo.ordered_workload import ordered_workload
from pqo.heavy_fresh_workload import heavy_fresh_workload


class OrderedWorkloadTests(unittest.TestCase):
    def test_unique_sql_and_disjoint_families(self):
        cases = list(ordered_workload())
        self.assertEqual(len(cases), 66)
        groups = {}
        texts = set()
        for split, case in cases:
            tree = parse_one(case.sql_text, read="postgres")
            self.assertIsInstance(tree, exp.Select)
            groups.setdefault(split, set()).add(case.template_id)
            texts.add(tree.sql(dialect="postgres", comments=False))
        self.assertEqual(len(texts), 66)
        self.assertEqual({key: len(value) for key, value in groups.items()},
                         {"train": 10, "validation": 4, "acceptance": 8})
        self.assertFalse(groups["train"] & groups["validation"])
        self.assertFalse(groups["train"] & groups["acceptance"])
        self.assertFalse(groups["validation"] & groups["acceptance"])
        old = {parse_one(case.sql_text, read="postgres").sql(dialect="postgres", comments=False)
               for _, case in heavy_fresh_workload()}
        self.assertFalse(texts & old)

    def test_training_includes_ordered_and_window_patterns(self):
        training = [case.sql_text for split, case in ordered_workload() if split == "train"]
        self.assertTrue(any("percentile_cont(ARRAY" in sql for sql in training))
        self.assertTrue(any("COUNT(DISTINCT" in sql and "OVER(" in sql for sql in training))
