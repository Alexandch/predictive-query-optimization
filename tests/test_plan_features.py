import unittest
from unittest.mock import patch

from pqo.config import DatabaseSettings
from pqo.explain import _assert_read_only_query
from pqo.plan_features import extract_plan_features


class PlanFeatureTests(unittest.TestCase):
    def test_flows_use_child_estimates_not_operator_output_or_actual_rows(self):
        scan = {"Node Type": "Seq Scan", "Plan Rows": 10000, "Plan Width": 40,
                "Actual Rows": 999999}
        aggregate = {"Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 20,
                     "Plan Width": 16, "Plans": [scan]}
        sort = {"Node Type": "Incremental Sort", "Plan Rows": 20, "Plan Width": 16,
                "Sort Key": ["revenue DESC"], "Plans": [aggregate]}
        window = {"Node Type": "WindowAgg", "Plan Rows": 20, "Plan Width": 24,
                  "Plans": [sort, {"Node Type": "Result", "Parent Relationship": "InitPlan",
                                   "Plan Rows": 100000, "Plan Width": 100}]}
        values = extract_plan_features({"Plan": window})
        self.assertEqual(values.aggregate_input_rows_sum, 10000)
        self.assertEqual(values.aggregate_input_bytes_sum, 400000)
        self.assertEqual(values.sort_input_rows_sum, 20)
        self.assertEqual(values.sort_input_bytes_max, 320)
        self.assertEqual(values.window_input_rows_sum, 20)
        self.assertEqual(values.window_input_bytes_sum, 320)
        self.assertEqual(values.window_agg_node_count, 1)
        self.assertEqual(values.incremental_sort_node_count, 1)
        self.assertEqual(values.hashed_aggregate_node_count, 1)
        self.assertEqual(values.distinct_sort_signature_count, 1)

    def test_flow_signature_deduplication_and_empty_defaults(self):
        child = {"Node Type": "Seq Scan", "Plan Rows": 100, "Plan Width": 8}
        inner = {"Node Type": "Sort", "Plan Rows": 100, "Plan Width": 8,
                 "Sort Key": ["a"], "Plans": [child]}
        outer = {"Node Type": "Sort", "Sort Key": ["a"], "Plans": [inner]}
        values = extract_plan_features({"Plan": outer})
        self.assertEqual(values.sort_input_rows_sum, 200)
        self.assertEqual(values.sort_input_rows_max, 100)
        self.assertEqual(values.distinct_sort_signature_count, 1)
        empty = extract_plan_features({"Plan": {"Node Type": "Result"}})
        from pqo.plan_features import PLAN_FLOW_FEATURES
        self.assertTrue(all(empty.as_dict()[name] == 0 for name in PLAN_FLOW_FEATURES))

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
