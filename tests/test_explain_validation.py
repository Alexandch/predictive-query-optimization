import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pqo.explain import _assert_read_only_query, measure_query_execution


class ExplainValidationTests(unittest.TestCase):
    def test_accepts_select_and_with_select(self):
        self.assertEqual(_assert_read_only_query("SELECT 1"), "SELECT 1")
        self.assertTrue(
            _assert_read_only_query("WITH x AS (SELECT 1) SELECT * FROM x")
        )

    def test_rejects_data_modifying_cte(self):
        with self.assertRaises(ValueError):
            _assert_read_only_query(
                "WITH deleted AS (DELETE FROM aviation.flights RETURNING *) "
                "SELECT * FROM deleted"
            )

    def test_rejects_multiple_statements(self):
        with self.assertRaises(ValueError):
            _assert_read_only_query("SELECT 1; SELECT 2")

    def test_measurement_discards_warmup_and_reports_median_range(self):
        values = iter((99.0, 8.0, 4.0, 6.0, 5.0, 7.0))

        def fake_collect(*args, **kwargs):
            return SimpleNamespace(
                features=SimpleNamespace(actual_total_time_ms=next(values)),
                plan_json=[],
            )

        with patch("pqo.explain.collect_explain", side_effect=fake_collect) as collect:
            result = measure_query_execution(
                "SELECT 1", connection=object(), warmup_runs=1, repetitions=5
            )

        self.assertEqual(collect.call_count, 6)
        self.assertEqual(result.samples_ms, (8.0, 4.0, 6.0, 5.0, 7.0))
        self.assertEqual(result.median_time_ms, 6.0)
        self.assertEqual(result.minimum_time_ms, 4.0)
        self.assertEqual(result.maximum_time_ms, 8.0)


if __name__ == "__main__":
    unittest.main()
