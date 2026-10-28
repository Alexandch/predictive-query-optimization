import unittest

from sqlglot import parse_one

from pqo.broad_workload import broad_workload
from pqo.complex_workload import complex_workload
from pqo.explain import _assert_read_only_query


class BroadWorkloadTests(unittest.TestCase):
    def test_fixed_disjoint_families_and_fresh_sql(self):
        cases = list(broad_workload())
        self.assertEqual(len(cases), 48)
        families = {}
        identities = set()
        for split, case in cases:
            families.setdefault(split, set()).add(case.template_id)
            sql = _assert_read_only_query(case.sql_text)
            identities.add(parse_one(sql, read="postgres").sql(dialect="postgres"))
            self.assertNotIn("logistics.", sql)
            self.assertNotIn("chbenchmark.", sql)
        self.assertEqual(len(identities), 48)
        self.assertEqual({k: len(v) for k, v in families.items()},
                         {"train": 8, "validation": 4, "acceptance": 4})
        for a, b in (("train", "validation"), ("train", "acceptance"), ("validation", "acceptance")):
            self.assertFalse(families[a] & families[b])
        previous = {parse_one(case.sql_text, read="postgres").sql(dialect="postgres")
                    for _, case in complex_workload()}
        self.assertFalse(identities & previous)
