import unittest

from sqlglot import parse_one

from pqo.paired_regression import assess, cluster_interval
from pqo.paired_workload import paired_workload


class PairedRegressionTests(unittest.TestCase):
    def rows(self):
        rows = []
        for cohort in ("fresh", "known_retained", "known_dropped"):
            for family in range(12):
                rows.append({"status": "ok", "cohort": cohort, "family": f"family_{family}",
                             "actual_ms": 100., "baseline_ms": 150., "candidate_ms": 110.})
        return rows

    def test_paired_improvement_passes_without_promoting(self):
        report = assess(self.rows(), 36)
        self.assertTrue(report["paired_quality_pass"])
        self.assertFalse(report["automatic_promotion"])
        self.assertFalse(report["previous_internal_gate_overridden"])
        self.assertLess(report["fresh_paired_interval"]["upper_95_ms"], 0)

    def test_dropped_cohort_regression_cannot_be_hidden_by_fresh_improvement(self):
        rows = self.rows()
        for row in rows:
            if row["cohort"] == "known_dropped":
                row["candidate_ms"] = 300.
        report = assess(rows, 36)
        self.assertFalse(report["paired_quality_pass"])
        self.assertFalse(report["checks"]["known_dropped"])

    def test_incomplete_or_error_is_not_a_pass(self):
        self.assertFalse(assess(self.rows(), 37)["complete"])
        rows = self.rows()
        rows[0]["status"] = "error"
        report = assess(rows, 36)
        self.assertFalse(report["complete"])
        self.assertFalse(report["paired_quality_pass"])

    def test_bootstrap_clusters_scale_variants(self):
        rows = self.rows()[:12]
        first = cluster_interval(rows)
        self.assertEqual(first, cluster_interval(rows * 3))
        self.assertEqual(first["families"], 12)

    def test_fixed_parseable_control(self):
        cases = list(paired_workload())
        self.assertEqual(len(cases), 36)
        self.assertEqual(len({case.template_id for case in cases}), 12)
        self.assertEqual(len({parse_one(case.sql_text, read="postgres").sql(dialect="postgres")
                              for case in cases}), 36)
