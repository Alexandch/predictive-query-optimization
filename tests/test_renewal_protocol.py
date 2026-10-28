import unittest

import pandas as pd

from pqo.renewal_protocol import select_cases
from pqo.paired_workload import paired_workload


class RenewalProtocolTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([{"template_id": family, "sql_text": f"SELECT {number} AS {family}",
                              "estimated_total_cost": number * 10, "actual_total_time_ms": number * 90}
                             for family in ("a", "b") for number in range(10)])

    def test_cost_spread_and_family_parameter_holdout(self):
        cases = select_cases(self.frame(), set())
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(case["split"] == "train" for case in cases), 6)
        self.assertEqual(sum(case["split"] == "parameter_control" for case in cases), 2)
        self.assertEqual(len({case["sql"] for case in cases}), 8)

    def test_old_targets_do_not_affect_selection(self):
        frame = self.frame()
        first = select_cases(frame, set())
        frame["actual_total_time_ms"] = 1234567
        self.assertEqual(first, select_cases(frame, set()))

    def test_protected_sql_and_singleton_family(self):
        frame = self.frame().iloc[:1]
        case = select_cases(frame, set())[0]
        self.assertEqual(case["split"], "train")
        self.assertEqual(select_cases(frame, {case["sql"]}), [])

    def test_renewal_control_is_not_previous_paired_sql(self):
        old = {case.sql_text for case in paired_workload()}
        new = {case.sql_text for case in paired_workload(bounds=(4100, 17000, 51000))}
        self.assertEqual(len(new), 36)
        self.assertFalse(old & new)
