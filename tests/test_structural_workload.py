import unittest

from sqlglot import parse_one

from pqo.broad_workload import broad_workload
from pqo.complex_workload import complex_workload
from pqo.structural_workload import structural_workload
from pqo.sql_features import STRUCTURAL_SQL_FEATURES, extract_sql_features
from pqo.explain import _assert_read_only_query


class StructuralWorkloadTests(unittest.TestCase):
    def test_disjoint_families_and_fresh_queries(self):
        cases = list(structural_workload())
        self.assertEqual(len(cases), 36)
        normalize = lambda sql: parse_one(sql, read="postgres").sql(dialect="postgres")
        current = {normalize(_assert_read_only_query(case.sql_text)) for _, case in cases}
        self.assertEqual(len(current), 36)
        previous = {normalize(case.sql_text) for generator in (complex_workload, broad_workload)
                    for _, case in generator()}
        self.assertFalse(current & previous)
        families = {}
        for split, case in cases:
            families.setdefault(split, set()).add(case.template_id)
            features = extract_sql_features(case.sql_text).as_dict()
            self.assertTrue(any(features[name] for name in STRUCTURAL_SQL_FEATURES))
            self.assertNotIn("logistics.", case.sql_text)
            self.assertNotIn("chbenchmark.", case.sql_text)
        self.assertEqual({key: len(value) for key, value in families.items()},
                         {"train": 4, "validation": 4, "acceptance": 4})
        self.assertFalse(families["train"] & (families["validation"] | families["acceptance"]))
        self.assertFalse(families["validation"] & families["acceptance"])
