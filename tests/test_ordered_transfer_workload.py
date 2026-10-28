import unittest
from sqlglot import parse_one
from pqo.ordered_transfer_workload import ordered_transfer_workload
from pqo.ordered_workload import ordered_workload


class OrderedTransferTests(unittest.TestCase):
    def test_eight_fresh_control_families(self):
        cases = list(ordered_transfer_workload())
        normalize = lambda sql: parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)
        texts = {normalize(case.sql_text) for case in cases}
        self.assertEqual(len(texts), 24)
        self.assertEqual(len({case.template_id for case in cases}), 8)
        self.assertFalse(texts & {normalize(case.sql_text) for _, case in ordered_workload()})
