import unittest

from pqo.structural_advisor import analyze_query_structure


def rule_ids(sql, **kwargs):
    return {item.rule_id for item in analyze_query_structure(sql, **kwargs)}


class StructuralAdvisorTests(unittest.TestCase):
    def test_detects_cartesian_join(self):
        rules = rule_ids(
            "SELECT * FROM retail.customers c CROSS JOIN retail.products p"
        )

        self.assertIn("cartesian-join", rules)

    def test_detects_function_wrapped_join_key(self):
        rules = rule_ids(
            "SELECT * FROM retail.customers c JOIN retail.addresses a "
            "ON lower(c.email) = lower(a.city)"
        )

        self.assertIn("expression-on-join-key", rules)

    def test_detects_left_join_rejected_by_where(self):
        rules = rule_ids(
            "SELECT c.customer_id FROM retail.customers c "
            "LEFT JOIN retail.customer_orders o ON o.customer_id = c.customer_id "
            "WHERE o.order_status = 'paid'"
        )

        self.assertIn("left-join-null-rejected", rules)

    def test_does_not_flag_left_join_with_is_null_filter(self):
        rules = rule_ids(
            "SELECT c.customer_id FROM retail.customers c "
            "LEFT JOIN retail.customer_orders o ON o.customer_id = c.customer_id "
            "WHERE o.order_id IS NULL"
        )

        self.assertNotIn("left-join-null-rejected", rules)

    def test_detects_non_aggregate_having_filter(self):
        rules = rule_ids(
            "SELECT segment, COUNT(*) FROM retail.customers "
            "GROUP BY segment HAVING segment = 'vip' AND COUNT(*) > 10"
        )

        self.assertIn("non-aggregate-having-filter", rules)

    def test_detects_distinct_aggregate_after_large_join(self):
        plan = [{"Plan": {"Node Type": "Aggregate", "Total Cost": 5000,
                           "Plan Rows": 100, "Plans": [
                               {"Node Type": "Hash Join", "Plan Rows": 20000}
                           ]}}]
        rules = rule_ids(
            "SELECT COUNT(DISTINCT o.customer_id) "
            "FROM retail.customer_orders o JOIN retail.order_items i "
            "ON i.order_id = o.order_id",
            plan_json=plan,
        )

        self.assertIn("distinct-after-large-join", rules)

    def test_detects_large_offset(self):
        rules = rule_ids(
            "SELECT order_id FROM retail.customer_orders "
            "ORDER BY order_id OFFSET 5000 LIMIT 100"
        )

        self.assertIn("large-offset-pagination", rules)

    def test_detects_sort_spill_from_analyzed_plan(self):
        plan = [{"Plan": {"Node Type": "Sort", "Total Cost": 100,
                           "Plan Rows": 10000, "Sort Method": "external merge",
                           "Sort Space Type": "Disk"}}]
        rules = rule_ids(
            "SELECT * FROM retail.customer_orders ORDER BY total_amount",
            plan_json=plan,
        )

        self.assertIn("sort-spill-to-disk", rules)

    def test_detects_large_nested_loop(self):
        plan = [{"Plan": {"Node Type": "Nested Loop", "Total Cost": 10000,
                           "Plan Rows": 120000}}]
        rules = rule_ids(
            "SELECT * FROM retail.customer_orders o JOIN retail.order_items i "
            "ON i.order_id = o.order_id",
            plan_json=plan,
        )

        self.assertIn("large-nested-loop", rules)

    def test_detects_hash_aggregate_spill(self):
        plan = [{"Plan": {"Node Type": "Aggregate", "Strategy": "Hashed",
                           "Total Cost": 10000, "Plan Rows": 1000,
                           "HashAgg Batches": 8, "Disk Usage": 4096}}]
        rules = rule_ids(
            "SELECT order_status, COUNT(*) FROM retail.customer_orders "
            "GROUP BY order_status",
            plan_json=plan,
        )

        self.assertIn("aggregate-spill-to-disk", rules)

    def test_recommends_materialized_view_for_expensive_summary(self):
        plan = [{"Plan": {"Node Type": "Aggregate", "Total Cost": 25000,
                           "Plan Rows": 20}}]
        recommendations = analyze_query_structure(
            "SELECT o.order_status, SUM(i.quantity * i.unit_price) AS revenue "
            "FROM retail.customer_orders o JOIN retail.order_items i "
            "ON i.order_id = o.order_id GROUP BY o.order_status",
            plan_json=plan,
            predicted_time_ms=250,
        )
        materialized = next(
            item for item in recommendations
            if item.rule_id == "expensive-summary-materialization"
        )

        self.assertIn("CREATE MATERIALIZED VIEW", materialized.suggested_sql)
        self.assertIn("WITH NO DATA", materialized.suggested_sql)

    def test_rejects_data_changing_sql(self):
        with self.assertRaisesRegex(ValueError, "Only SELECT"):
            analyze_query_structure("DELETE FROM retail.customers")


if __name__ == "__main__":
    unittest.main()
