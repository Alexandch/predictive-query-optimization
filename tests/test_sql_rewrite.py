import unittest

from pqo.sql_rewrite import generate_sql_rewrites, evaluate_sql_variant


class SQLRewriteTests(unittest.TestCase):
    def rule(self, sql, rule):
        return next((item for item in generate_sql_rewrites(sql) if item.rule_id == rule), None)

    def test_scalar_aggregate_becomes_grouped_left_join(self):
        for aggregate in ("SUM(c.amount)", "AVG(c.amount)", "MIN(c.amount)", "MAX(c.amount)", "COUNT(*)", "COUNT(c.amount)"):
            with self.subTest(aggregate=aggregate):
                candidate = self.rule(f"SELECT p.id, (SELECT {aggregate} FROM retail.payments c "
                                      "WHERE c.order_id=p.id AND c.status='paid') AS total "
                                      "FROM retail.customer_orders p WHERE p.id > 10 ORDER BY p.id",
                                      "scalar-aggregate-to-grouped-join")
                self.assertIsNotNone(candidate)
                self.assertIn("LEFT JOIN", candidate.sql_text)
                self.assertIn("GROUP BY c.order_id", " ".join(candidate.sql_text.split()))
                self.assertIn("c.status = 'paid'", candidate.sql_text)
                self.assertEqual("COALESCE" in candidate.sql_text, aggregate.startswith("COUNT"))
                self.assertIn("AS total", candidate.sql_text)

    def test_scalar_multiple_aggregates_use_distinct_aliases(self):
        sql = ("SELECT p.id, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id) AS total, "
               "(SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id) AS n FROM retail.customer_orders p")
        candidate = self.rule(sql, "scalar-aggregate-to-grouped-join")
        self.assertIn("_pqo_agg_1", candidate.sql_text)
        self.assertIn("_pqo_agg_2", candidate.sql_text)
        self.assertEqual(candidate.sql_text.count("LEFT JOIN"), 2)

    def test_scalar_ambiguous_or_nonlocal_aggregates_are_not_rewritten(self):
        for projection, suffix in (
            ("SUM(c.amount)", "GROUP BY c.order_id"),
            ("SUM(c.amount)", "LIMIT 1"),
            ("COUNT(DISTINCT (c.amount,c.order_id))", ""),
            ("SUM(random())", ""),
            ("SUM(c.amount) FILTER (WHERE c.amount>1)", ""),
            ("SUM(c.amount) OVER ()", ""),
        ):
            sql = (f"SELECT p.id, (SELECT {projection} FROM retail.payments c WHERE c.order_id=p.id "
                   f"{suffix}) AS n FROM retail.customer_orders p")
            self.assertIsNone(self.rule(sql, "scalar-aggregate-to-grouped-join"), sql)
        for sql in (
            "SELECT p.*, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id) AS n FROM retail.customer_orders p",
            "SELECT p.id, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id AND c.id>p.id) AS n FROM retail.customer_orders p",
            "SELECT p.id, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id OR c.status='paid') AS n FROM retail.customer_orders p",
            "SELECT p.id, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=id) AS n FROM retail.customer_orders p",
            "SELECT p.id, (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id AND random()>0.5) AS n FROM retail.customer_orders p",
        ):
            self.assertIsNone(self.rule(sql, "scalar-aggregate-to-grouped-join"), sql)

    def test_distinct_filtering_join_becomes_exists_but_keeps_distinct(self):
        candidate = self.rule("SELECT DISTINCT p.customer_id AS id FROM retail.customers p "
                              "JOIN retail.customer_orders c ON c.customer_id=p.customer_id "
                              "WHERE c.order_status='paid' AND p.customer_id>10 ORDER BY id",
                              "distinct-inner-join-to-exists")
        self.assertIsNotNone(candidate)
        self.assertIn("EXISTS", candidate.sql_text)
        self.assertIn("SELECT DISTINCT", candidate.sql_text)
        self.assertNotIn("JOIN", candidate.sql_text)
        self.assertIn("c.order_status = 'paid'", candidate.sql_text)
        self.assertIn("ORDER BY id", " ".join(candidate.sql_text.split()))

    def test_join_rewrite_excludes_outer_joins_child_outputs_and_unsafe_scopes(self):
        for sql in (
            "SELECT DISTINCT p.id FROM retail.customers p LEFT JOIN retail.payments c ON c.id=p.id",
            "SELECT DISTINCT p.id, c.id FROM retail.customers p JOIN retail.payments c ON c.id=p.id",
            "SELECT DISTINCT p.* FROM retail.customers p JOIN retail.payments c ON c.id=p.id",
            "SELECT DISTINCT ON (p.id) p.id FROM retail.customers p JOIN retail.payments c ON c.id=p.id",
            "SELECT DISTINCT p.id FROM retail.customers p JOIN retail.payments c USING(id)",
            "SELECT DISTINCT p.id FROM retail.customers p JOIN retail.payments c ON c.id=p.id LIMIT 5",
            "SELECT DISTINCT p.id FROM retail.customers p JOIN retail.payments c ON c.id=p.id WHERE random()>0.5",
            "SELECT DISTINCT id FROM retail.customers p JOIN retail.payments c ON c.id=p.id",
        ):
            self.assertIsNone(self.rule(sql, "distinct-inner-join-to-exists"), sql)

    def test_having_rollup_is_not_pushed_before_aggregation(self):
        for grouping in ("ROLLUP(customer_id)", "CUBE(customer_id)", "GROUPING SETS ((customer_id), ())"):
            self.assertIsNone(self.rule(f"SELECT customer_id, COUNT(*) FROM retail.customer_orders "
                                        f"GROUP BY {grouping} HAVING customer_id IS NOT NULL",
                                        "non-aggregate-having-filter"))

    def test_reused_cte_can_be_compared_with_inlining(self):
        candidates = generate_sql_rewrites(
            "WITH x AS (SELECT id FROM public.orders) SELECT a.id FROM x a JOIN x b ON a.id=b.id"
        )
        candidate = next(item for item in candidates if item.rule_id == "inline-reused-cte")
        self.assertIn("NOT MATERIALIZED", candidate.sql_text)

    def test_recursive_and_already_inline_ctes_are_not_rewritten(self):
        for sql in (
            "WITH RECURSIVE x AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM x WHERE n<3) SELECT * FROM x",
            "WITH x AS NOT MATERIALIZED (SELECT id FROM public.orders) SELECT * FROM x a JOIN x b ON a.id=b.id",
        ):
            self.assertFalse(any(item.rule_id == "inline-reused-cte" for item in generate_sql_rewrites(sql)))
    def test_zero_count_uses_not_exists(self):
        candidate = generate_sql_rewrites(
            "SELECT 1 WHERE (SELECT COUNT(*) FROM public.orders WHERE id = 7) = 0"
        )[0]
        self.assertEqual(candidate.rule_id, "count-zero-to-not-exists")
        self.assertIn("NOT EXISTS", candidate.sql_text)

    def test_count_column_is_not_rewritten_as_existence(self):
        self.assertFalse(generate_sql_rewrites(
            "SELECT 1 WHERE (SELECT COUNT(nullable_value) FROM public.orders) = 0"
        ))

    def test_preserves_order_with_offset_and_distinct_on(self):
        for inner in (
            "SELECT id FROM public.orders ORDER BY id OFFSET 10",
            "SELECT DISTINCT ON (customer_id) customer_id,id FROM public.orders ORDER BY customer_id,id DESC",
        ):
            self.assertFalse(any(item.rule_id == "subquery-order-without-limit"
                                 for item in generate_sql_rewrites(f"SELECT * FROM ({inner}) q")))

    def test_global_aggregate_having_is_not_pushed_down(self):
        self.assertFalse(generate_sql_rewrites("SELECT COUNT(*) FROM public.orders HAVING false"))

    def test_manual_variant_rejects_mutation_and_identical_sql_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "Only SELECT"):
            evaluate_sql_variant("SELECT 1", "DELETE FROM public.orders")
        with self.assertRaisesRegex(ValueError, "совпадает"):
            evaluate_sql_variant("SELECT 1", "SELECT\n 1;")

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

    def test_removes_order_from_subquery_without_limit(self):
        candidates = generate_sql_rewrites(
            "SELECT q.id FROM (SELECT id FROM public.orders ORDER BY id) AS q"
        )

        candidate = next(
            item
            for item in candidates
            if item.rule_id == "subquery-order-without-limit"
        )
        self.assertNotIn("ORDER BY", candidate.sql_text)

    def test_moves_non_aggregate_having_term_to_where(self):
        candidates = generate_sql_rewrites(
            "SELECT customer_id, COUNT(*) FROM public.orders "
            "GROUP BY customer_id HAVING customer_id > 10 AND COUNT(*) > 1"
        )

        candidate = next(
            item
            for item in candidates
            if item.rule_id == "non-aggregate-having-filter"
        )
        self.assertIn("WHERE", candidate.sql_text)
        self.assertIn("HAVING", candidate.sql_text)
        self.assertIn("COUNT(*) > 1", candidate.sql_text)


if __name__ == "__main__":
    unittest.main()
