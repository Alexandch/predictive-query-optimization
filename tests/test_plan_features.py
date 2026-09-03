import unittest
from unittest.mock import patch

from pqo.config import DatabaseSettings
from pqo.explain import _assert_read_only_query
from pqo.plan_features import extract_plan_features


class PlanFeatureTests(unittest.TestCase):
    def test_extracts_and_aggregates_nested_plan_features(self):
        explain_json = [
            {
                "Plan": {
                    "Node Type": "Hash Join",
                    "Total Cost": 42.75,
                    "Plan Rows": 12,
                    "Actual Total Time": 1.25,
                    "Shared Hit Blocks": 2,
                    "Shared Read Blocks": 1,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Total Cost": 10,
                            "Plan Rows": 100,
                            "Shared Hit Blocks": 4,
                            "Shared Read Blocks": 3,
                            "Temp Read Blocks": 1,
                        },
                        {
                            "Node Type": "Hash",
                            "Total Cost": 15,
                            "Plan Rows": 20,
                            "Shared Hit Blocks": 5,
                            "Temp Written Blocks": 2,
                        },
                    ],
                }
            }
        ]

        features = extract_plan_features(explain_json)

        self.assertEqual(features.root_node_type, "Hash Join")
        self.assertEqual(features.node_count, 3)
        self.assertEqual(features.max_plan_depth, 2)
        self.assertEqual(features.estimated_total_cost, 42.75)
        self.assertEqual(features.estimated_plan_rows, 12.0)
        self.assertEqual(features.actual_total_time_ms, 1.25)
        self.assertEqual(features.shared_hit_blocks, 11)
        self.assertEqual(features.shared_read_blocks, 4)
        self.assertEqual(features.temp_read_blocks, 1)
        self.assertEqual(features.temp_written_blocks, 2)
        self.assertEqual(features.seq_scan_count, 1)
        self.assertEqual(features.hash_join_count, 1)

    def test_rejects_invalid_explain_shape(self):
        with self.assertRaisesRegex(ValueError, "top-level 'Plan'"):
            extract_plan_features({"not_plan": {}})

    def test_accepts_select_and_with_queries(self):
        self.assertEqual(_assert_read_only_query(" SELECT 1; "), "SELECT 1")
        self.assertEqual(
            _assert_read_only_query("WITH value AS (SELECT 1) SELECT * FROM value"),
            "WITH value AS (SELECT 1) SELECT * FROM value",
        )

    def test_rejects_mutating_queries(self):
        with self.assertRaisesRegex(ValueError, "Only SELECT and WITH"):
            _assert_read_only_query("DELETE FROM bookings")

    def test_rejects_multiple_statements(self):
        with self.assertRaisesRegex(ValueError, "Only one SQL statement"):
            _assert_read_only_query("SELECT 1; SELECT 2")

    def test_database_settings_have_reproducible_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = DatabaseSettings.from_env()

        self.assertEqual(settings.host, "localhost")
        self.assertEqual(settings.port, 55432)
        self.assertEqual(settings.dbname, "query_optimizer")


if __name__ == "__main__":
    unittest.main()
