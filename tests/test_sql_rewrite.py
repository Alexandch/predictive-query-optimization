import unittest

from pqo.sql_rewrite import generate_sql_rewrites


class SQLRewriteTests(unittest.TestCase):
    def test_rewrites_positive_correlated_count_to_exists(self):
        candidates = generate_sql_rewrites(
            "SELECT c.customer_id FROM retail.customers c "
            "WHERE (SELECT COUNT(*) FROM retail.customer_orders o "
            "WHERE o.customer_id = c.customer_id) > 0"
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rule_id, "count-positive-to-exists")
        self.assertIn("EXISTS(", candidates[0].sql_text)
        self.assertNotIn("COUNT", candidates[0].sql_text)

    def test_does_not_rewrite_grouped_count(self):
        candidates = generate_sql_rewrites(
            "SELECT 1 WHERE (SELECT COUNT(*) FROM public.orders "
            "GROUP BY customer_id) > 0"
        )

        self.assertEqual(candidates, ())

    def test_rewrites_reversed_positive_count_comparison(self):
        candidates = generate_sql_rewrites(
            "SELECT 1 WHERE 0 < (SELECT COUNT(*) FROM public.orders)"
        )

        self.assertEqual(candidates[0].rule_id, "count-positive-to-exists")

    def test_does_not_rewrite_count_less_than_zero(self):
        candidates = generate_sql_rewrites(
            "SELECT 1 WHERE 0 > (SELECT COUNT(*) FROM public.orders)"
        )

        self.assertEqual(candidates, ())

    def test_rewrites_same_column_or_equalities_to_in(self):
        candidates = generate_sql_rewrites(
            "SELECT * FROM public.orders "
            "WHERE status = 'new' OR status = 'paid' OR status = 'sent'"
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rule_id, "or-equality-to-in")
        self.assertIn("status IN ('new', 'paid', 'sent')", candidates[0].sql_text)

    def test_does_not_merge_equalities_for_different_columns(self):
        candidates = generate_sql_rewrites(
            "SELECT * FROM public.orders "
            "WHERE status = 'new' OR customer_id = 10"
        )

        self.assertEqual(candidates, ())

    def test_rewrites_inclusive_date_range_to_between(self):
        candidates = generate_sql_rewrites(
            "SELECT * FROM public.orders "
            "WHERE created_at >= DATE '2026-01-01' "
            "AND created_at <= DATE '2026-12-31'"
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rule_id, "range-to-between")
        self.assertIn(
            "created_at BETWEEN CAST('2026-01-01' AS DATE) "
            "AND CAST('2026-12-31' AS DATE)",
            candidates[0].sql_text,
        )

    def test_does_not_rewrite_exclusive_range(self):
        candidates = generate_sql_rewrites(
            "SELECT * FROM public.orders WHERE amount > 10 AND amount < 20"
        )

        self.assertEqual(candidates, ())

    def test_keeps_read_only_validation(self):
        with self.assertRaisesRegex(ValueError, "Only SELECT"):
            generate_sql_rewrites("DELETE FROM public.orders")


if __name__ == "__main__":
    unittest.main()
