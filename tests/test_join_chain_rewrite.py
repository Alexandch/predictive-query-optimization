import unittest
from unittest.mock import Mock
from sqlglot import exp, parse_one
from pqo.sql_rewrite import generate_sql_rewrites, _queries_are_equivalent


class JoinChainRewriteTests(unittest.TestCase):
    def test_equivalence_checks_both_directions_sequentially(self):
        connection = Mock()
        connection.execute.return_value.fetchone.side_effect = [(True,), (False,)]
        self.assertFalse(_queries_are_equivalent(connection, "SELECT 1", "SELECT 1 UNION ALL SELECT 1"))
        self.assertEqual(connection.execute.call_count, 2)
        first, second = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("EXCEPT ALL", first)
        self.assertIn("EXCEPT ALL", second)
        self.assertEqual(first.count("EXCEPT ALL"), 1)
        self.assertEqual(second.count("EXCEPT ALL"), 1)

    def test_equivalence_stops_at_first_mismatch(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (False,)
        self.assertFalse(_queries_are_equivalent(connection, "SELECT 1", "SELECT 2"))
        self.assertEqual(connection.execute.call_count, 1)

    def test_later_outer_references_are_lifted_without_losing_child_conditions(self):
        candidate = self.candidate("""SELECT DISTINCT p.id FROM retail.customers p
            JOIN retail.customer_orders o ON o.customer_id=p.id
            JOIN retail.order_items i ON i.order_id=o.order_id AND i.owner=p.id
            JOIN retail.payments pay ON pay.owner=p.id WHERE i.quantity>0""")
        inner = next(parse_one(candidate.sql_text, read="postgres").find_all(exp.Exists)).this
        self.assertIn("i.order_id = o.order_id", inner.args["joins"][0].args["on"].sql())
        self.assertNotIn("p.id", inner.args["joins"][0].args["on"].sql())
        self.assertEqual(inner.args["joins"][1].args["kind"], "CROSS")
        self.assertIn("i.owner = p.id", inner.args["where"].sql())
        self.assertIn("pay.owner = p.id", inner.args["where"].sql())

    def test_table_functions_are_not_treated_as_plain_sources(self):
        for sql in (
            "SELECT DISTINCT p.id FROM retail.customers p JOIN generate_series(1,3) o ON o.x=p.id",
            "SELECT DISTINCT p.id FROM generate_series(1,3) p JOIN retail.customer_orders o ON o.customer_id=p.id",
        ):
            self.assertIsNone(self.candidate(sql))

    def candidate(self, sql):
        return next((item for item in generate_sql_rewrites(sql)
                     if item.rule_id == "distinct-inner-join-to-exists"), None)

    def test_chain_is_moved_into_one_exists_with_child_dependency(self):
        sql = """SELECT DISTINCT p.id FROM retail.customers p
            JOIN retail.customer_orders o ON o.customer_id=p.id
            JOIN retail.order_items i ON i.order_id=o.order_id
            JOIN retail.products pr ON pr.product_id=i.product_id
            WHERE pr.is_active=true AND i.quantity>1 ORDER BY p.id"""
        candidate = self.candidate(sql)
        self.assertIsNotNone(candidate)
        tree = parse_one(candidate.sql_text, read="postgres")
        self.assertIsNone(tree.args.get("joins"))
        exists = list(tree.find_all(exp.Exists))
        self.assertEqual(len(exists), 1)
        self.assertEqual(len(exists[0].this.args["joins"]), 2)
        self.assertIn("i.order_id = o.order_id", candidate.sql_text)
        self.assertIn("pr.product_id = i.product_id", candidate.sql_text)
        self.assertIn("SELECT DISTINCT", candidate.sql_text)

    def test_parent_child_or_filter_stays_inside_shared_exists(self):
        candidate = self.candidate("""SELECT DISTINCT p.id FROM retail.customers p
            JOIN retail.customer_orders o ON o.customer_id=p.id
            JOIN retail.order_items i ON i.order_id=o.order_id
            WHERE p.id=2 OR i.quantity>1""")
        exists = next(parse_one(candidate.sql_text, read="postgres").find_all(exp.Exists))
        self.assertIn("p.id = 2 OR i.quantity > 1", exists.this.args["where"].sql())

    def test_any_unsafe_join_rejects_whole_chain(self):
        for last in (
            "LEFT JOIN retail.order_items i ON i.order_id=o.order_id",
            "FULL JOIN retail.order_items i ON i.order_id=o.order_id",
            "CROSS JOIN retail.order_items i",
            "JOIN retail.order_items i USING(order_id)",
            "NATURAL JOIN retail.order_items i",
            "JOIN retail.order_items o ON o.order_id=p.id",
            "JOIN retail.order_items i ON random()>0.5",
            "JOIN (SELECT order_id FROM retail.order_items) i ON i.order_id=o.order_id",
        ):
            self.assertIsNone(self.candidate(f"SELECT DISTINCT p.id FROM retail.customers p JOIN retail.customer_orders o ON o.customer_id=p.id {last}"), last)

    def test_no_distinct_or_child_outputs_are_not_rewritten(self):
        tail = "FROM retail.customers p JOIN retail.customer_orders o ON o.customer_id=p.id JOIN retail.order_items i ON i.order_id=o.order_id"
        for projection in ("p.id", "DISTINCT p.id,i.product_id", "DISTINCT p.*", "DISTINCT ON(p.id) p.id"):
            self.assertIsNone(self.candidate(f"SELECT {projection} {tail}"))

    def test_forward_alias_and_unqualified_columns_are_rejected(self):
        for sql in (
            "SELECT DISTINCT p.id FROM retail.customers p JOIN retail.customer_orders o ON o.order_id=i.order_id JOIN retail.order_items i ON i.order_id=o.order_id",
            "SELECT DISTINCT p.id FROM retail.customers p JOIN retail.customer_orders o ON o.customer_id=p.id JOIN retail.order_items i ON i.order_id=order_id",
        ):
            self.assertIsNone(self.candidate(sql))

    def test_order_alias_and_quoted_relation_aliases_are_preserved(self):
        candidate = self.candidate('''SELECT DISTINCT "P".id AS customer FROM retail.customers "P"
            JOIN retail.customer_orders "O" ON "O".customer_id="P".id
            JOIN retail.order_items "I" ON "I".order_id="O".order_id ORDER BY customer''')
        self.assertIn('"O".customer_id = "P".id', candidate.sql_text)
        self.assertIn("ORDER BY", candidate.sql_text)
