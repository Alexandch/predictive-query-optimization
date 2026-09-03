import unittest

from pqo.sql_features import extract_sql_features


class SQLFeatureTests(unittest.TestCase):
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
