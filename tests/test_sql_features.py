import unittest

from pqo.sql_features import extract_sql_features


class SQLFeatureTests(unittest.TestCase):
    def test_grouping_sets_are_counted_without_changing_v2_group_keys(self):
        features = extract_sql_features("""SELECT a,b,SUM(c) FROM t
            GROUP BY GROUPING SETS ((a,b),(a),()), ROLLUP(a,b), CUBE(a,b)""")
        self.assertEqual(features.grouping_sets_clause_count, 1)
        self.assertEqual(features.rollup_count, 1)
        self.assertEqual(features.cube_count, 1)
        self.assertEqual(features.grouping_set_expansion_count, 3 * 3 * 4)
        self.assertEqual(features.advanced_group_key_count, 7)
        self.assertEqual(features.group_key_count, 0)  # frozen SQL-v2 contract
        self.assertEqual(extract_sql_features("SELECT a FROM t GROUP BY a").grouping_set_expansion_count, 1)
        self.assertEqual(extract_sql_features("SELECT 1").grouping_set_expansion_count, 0)

    def test_grouping_expansion_is_bounded_and_handles_composite_keys(self):
        from pqo.sql_features import GROUPING_EXPANSION_LIMIT
        features = extract_sql_features("SELECT a FROM t GROUP BY CUBE(" + ",".join(f"c{i}" for i in range(100)) + ")")
        self.assertEqual(features.grouping_set_expansion_count, GROUPING_EXPANSION_LIMIT)
        composite = extract_sql_features("SELECT a FROM t GROUP BY ROLLUP((a,b),c)")
        self.assertEqual(composite.grouping_set_expansion_count, 3)

    def test_ordered_sets_and_explicit_window_frames(self):
        features = extract_sql_features("""SELECT
            percentile_cont(0.5) WITHIN GROUP(ORDER BY v),
            sum(v) OVER(ORDER BY a ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
            avg(v) OVER(ORDER BY a RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
            sum(v) OVER(ORDER BY a GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE TIES)
            FROM t""")
        self.assertEqual(features.ordered_set_aggregate_count, 1)
        self.assertEqual(features.ordered_set_order_key_count, 1)
        self.assertEqual(features.window_rows_frame_count, 1)
        self.assertEqual(features.window_range_frame_count, 1)
        self.assertEqual(features.window_groups_frame_count, 1)
        self.assertEqual(features.window_bounded_frame_count, 2)
        self.assertEqual(features.window_unbounded_frame_count, 1)
        self.assertEqual(features.window_exclusion_count, 1)

    def test_named_frame_is_counted_once_at_definition(self):
        features = extract_sql_features("""SELECT sum(v) OVER w, avg(v) OVER w FROM t
            WINDOW w AS (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)""")
        self.assertEqual(features.window_rows_frame_count, 1)

    def test_separates_ctes_windows_and_distinct_aggregates(self):
        query = """
            WITH summary AS MATERIALIZED (
                SELECT product_id, COUNT(DISTINCT order_id) AS orders,
                       SUM(quantity) FILTER (WHERE quantity > 1) AS units
                FROM retail.order_items GROUP BY product_id
            )
            SELECT product_id, DENSE_RANK() OVER (ORDER BY orders DESC),
                   AVG(orders) OVER (PARTITION BY units)
            FROM summary
        """
        features = extract_sql_features(query)
        self.assertEqual(features.cte_count, 1)
        self.assertEqual(features.materialized_cte_count, 1)
        self.assertEqual(features.cte_reference_count, 1)
        self.assertEqual(features.window_function_count, 2)
        self.assertEqual(features.window_aggregate_count, 1)
        self.assertEqual(features.ranking_window_count, 1)
        self.assertEqual(features.distinct_aggregate_count, 1)
        self.assertEqual(features.aggregate_filter_count, 1)
        self.assertEqual(features.group_key_count, 1)

    def test_extracts_complex_query_features(self):
        query = """
            SELECT DISTINCT b.book_ref, COUNT(*)
            FROM bookings AS b
            LEFT JOIN tickets AS t ON t.book_ref = b.book_ref
            WHERE b.total_amount > 100
              AND EXISTS (
                  SELECT 1
                  FROM ticket_flights AS tf
                  WHERE tf.ticket_no = t.ticket_no
              )
            GROUP BY b.book_ref
            ORDER BY b.book_ref
        """

        features = extract_sql_features(query)

        self.assertEqual(features.join_count, 1)
        self.assertEqual(features.left_join_count, 1)
        self.assertEqual(features.where_condition_count, 3)
        self.assertEqual(features.subquery_count, 1)
        self.assertTrue(features.has_group_by)
        self.assertTrue(features.has_order_by)
        self.assertTrue(features.has_distinct)
        self.assertEqual(features.table_reference_count, 3)
        self.assertEqual(features.unique_table_count, 3)
        self.assertEqual(features.select_expression_count, 3)
        self.assertEqual(features.aggregate_function_count, 1)

    def test_classifies_join_types(self):
        query = """
            SELECT *
            FROM a
            JOIN b ON b.id = a.id
            RIGHT JOIN c ON c.id = b.id
            FULL JOIN d ON d.id = c.id
            CROSS JOIN e
        """

        features = extract_sql_features(query)

        self.assertEqual(features.join_count, 4)
        self.assertEqual(features.inner_join_count, 1)
        self.assertEqual(features.right_join_count, 1)
        self.assertEqual(features.full_join_count, 1)
        self.assertEqual(features.cross_join_count, 1)

    def test_cte_name_is_not_counted_as_physical_table(self):
        query = """
            WITH recent AS (
                SELECT book_ref FROM bookings WHERE total_amount > 100
            )
            SELECT recent.book_ref
            FROM recent
            JOIN tickets ON tickets.book_ref = recent.book_ref
        """

        features = extract_sql_features(query)

        self.assertEqual(features.table_reference_count, 2)
        self.assertEqual(features.unique_table_count, 2)
        self.assertEqual(features.subquery_count, 1)

    def test_rejects_multiple_statements(self):
        with self.assertRaisesRegex(ValueError, "Exactly one SQL statement"):
            extract_sql_features("SELECT 1; SELECT 2")


if __name__ == "__main__":
    unittest.main()
