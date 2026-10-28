import unittest
from sqlglot import exp, parse_one
from pqo.sql_rewrite import generate_sql_rewrites, sql_rewrite_rules


RULE = "shared-scalar-aggregates-to-grouped-join"


class SharedScalarRewriteTests(unittest.TestCase):
    def test_registry_ids_are_unique_and_shared_rule_is_listed(self):
        rules = sql_rewrite_rules()
        self.assertEqual(len({item[0] for item in rules}), len(rules))
        self.assertIn(RULE, {item[0] for item in rules})

    def candidate(self, sql):
        return next((item for item in generate_sql_rewrites(sql) if item.rule_id == RULE), None)

    def test_different_child_aliases_share_one_grouping(self):
        sql = """SELECT p.id,
            (SELECT SUM(a.amount) FROM retail.payments a WHERE a.order_id=p.id AND a.status='paid') AS total,
            (SELECT COUNT(*) FROM retail.payments b WHERE b.order_id=p.id AND b.status='paid') AS n,
            (SELECT AVG(c.amount) FROM retail.payments c WHERE c.order_id=p.id AND c.status='paid') AS average
            FROM retail.customer_orders p ORDER BY p.id"""
        candidate = self.candidate(sql)
        self.assertIsNotNone(candidate)
        tree = parse_one(candidate.sql_text, read="postgres")
        self.assertEqual(len(tree.args["joins"]), 1)
        self.assertEqual(len(list(tree.find_all(exp.Group))), 1)
        self.assertIn("COALESCE(_pqo_agg_1._pqo_value_2, 0)", candidate.sql_text)
        self.assertIn("AVG(a.amount)", candidate.sql_text)
        self.assertNotIn("b.amount", candidate.sql_text)

    def test_distinct_filters_relations_and_parent_keys_are_not_combined(self):
        for second in (
            "SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id AND c.status='new'",
            "SELECT COUNT(*) FROM retail.order_items c WHERE c.order_id=p.id AND c.status='paid'",
            "SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.other_id AND c.status='paid'",
        ):
            sql = f"""SELECT p.id,
                (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id AND c.status='paid') AS total,
                ({second}) AS n FROM retail.customer_orders p"""
            self.assertIsNone(self.candidate(sql))

    def test_single_aggregate_does_not_produce_duplicate_candidate(self):
        self.assertIsNone(self.candidate("SELECT p.id,(SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id) AS total FROM retail.customer_orders p"))

    def test_partial_merge_retains_other_source(self):
        sql = """SELECT p.id,
            (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id) AS n,
            (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id) AS total,
            (SELECT COUNT(*) FROM retail.order_items c WHERE c.order_id=p.id) AS lines
            FROM retail.customer_orders p"""
        candidate = self.candidate(sql)
        self.assertEqual(len(parse_one(candidate.sql_text, read="postgres").args["joins"]), 2)
        self.assertEqual(candidate.sql_text.count("COALESCE"), 2)

    def test_original_independent_candidate_is_still_available(self):
        sql = """SELECT p.id,(SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id) AS n,
            (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=p.id) AS total
            FROM retail.customer_orders p"""
        candidates = generate_sql_rewrites(sql)
        self.assertIn("scalar-aggregate-to-grouped-join", {item.rule_id for item in candidates})
        self.assertIn(RULE, {item.rule_id for item in candidates})

    def test_existing_generated_style_alias_is_not_reused(self):
        candidate = self.candidate("""SELECT _pqo_agg_1.id,
            (SELECT SUM(c.amount) FROM retail.payments c WHERE c.order_id=_pqo_agg_1.id) AS total,
            (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=_pqo_agg_1.id) AS n
            FROM retail.customer_orders _pqo_agg_1""")
        self.assertIn("AS _pqo_agg_2", candidate.sql_text)

    def test_unsafe_scalar_shapes_remain_excluded(self):
        for aggregate in ("COUNT(DISTINCT (c.amount,c.order_id))", "SUM(random())", "SUM(c.amount) FILTER(WHERE c.amount>0)"):
            sql = f"""SELECT p.id, (SELECT {aggregate} FROM retail.payments c WHERE c.order_id=p.id) AS x,
                (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id) AS n FROM retail.customer_orders p"""
            self.assertIsNone(self.candidate(sql))

    def test_quoted_child_alias_is_preserved(self):
        candidate = self.candidate('''SELECT p.id,
            (SELECT SUM("Child".amount) FROM retail.payments "Child" WHERE "Child".order_id=p.id) AS total,
            (SELECT COUNT(x.amount) FROM retail.payments x WHERE x.order_id=p.id) AS n
            FROM retail.customer_orders p''')
        self.assertIn('COUNT("Child".amount)', candidate.sql_text)

    def test_different_table_column_alias_lists_are_not_combined(self):
        sql = """SELECT p.id,
            (SELECT SUM(c.amount) FROM retail.payments c(order_id,amount) WHERE c.order_id=p.id) AS total,
            (SELECT COUNT(x.amount) FROM retail.payments x(amount,order_id) WHERE x.order_id=p.id) AS n
            FROM retail.customer_orders p"""
        self.assertIsNone(self.candidate(sql))

    def test_composite_keys_and_distinct_share_one_grouping_despite_reversed_terms(self):
        sql = """SELECT p.id,p.status,
            (SELECT COUNT(DISTINCT c.amount) FROM retail.payments c
             WHERE c.order_id=p.id AND c.status=p.status) AS unique_amounts,
            (SELECT SUM(x.amount) FROM retail.payments x
             WHERE p.status=x.status AND p.id=x.order_id) AS total
            FROM retail.customer_orders p"""
        candidate = self.candidate(sql)
        self.assertIsNotNone(candidate)
        tree = parse_one(candidate.sql_text, read="postgres")
        self.assertEqual(len(tree.args["joins"]), 1)
        self.assertEqual(len(next(tree.find_all(exp.Group)).expressions), 2)
        self.assertIn("COUNT(DISTINCT c.amount)", candidate.sql_text)
        self.assertIn("_pqo_key_2 = p.status", candidate.sql_text)
        self.assertIn("COALESCE", candidate.sql_text)

    def test_different_second_parent_key_is_not_merged(self):
        self.assertIsNone(self.candidate("""SELECT p.id,
            (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id AND c.status=p.status) AS n,
            (SELECT SUM(x.amount) FROM retail.payments x WHERE x.order_id=p.id AND x.status=p.other_status) AS total
            FROM retail.customer_orders p"""))

    def test_distinct_tuple_or_nonlocal_expression_is_not_rewritten(self):
        for aggregate in ("COUNT(DISTINCT c.a,c.b)", "COUNT(DISTINCT (c.a,c.b))",
                          "COUNT(DISTINCT p.id)", "COUNT(DISTINCT random())", "SUM(DISTINCT c.amount)"):
            sql = f"""SELECT p.id, (SELECT {aggregate} FROM retail.payments c WHERE c.order_id=p.id) AS n
                FROM retail.customer_orders p"""
            self.assertFalse(any(item.rule_id == "scalar-aggregate-to-grouped-join"
                                 for item in generate_sql_rewrites(sql)), aggregate)

    def test_non_equality_or_expression_composite_correlations_are_excluded(self):
        for predicate in ("c.kind>p.kind", "c.kind IS NOT DISTINCT FROM p.kind",
                          "c.kind=p.kind OR c.kind IS NULL", "c.kind=p.kind+1"):
            sql = f"""SELECT p.id,(SELECT COUNT(DISTINCT c.amount) FROM retail.payments c
                WHERE c.order_id=p.id AND ({predicate})) AS n FROM retail.customer_orders p"""
            self.assertFalse(any(item.rule_id == "scalar-aggregate-to-grouped-join"
                                 for item in generate_sql_rewrites(sql)), predicate)

    def test_multi_key_different_child_column_does_not_share_group(self):
        sql = """SELECT p.id,
            (SELECT COUNT(*) FROM retail.payments c WHERE c.order_id=p.id AND c.kind=p.kind) AS n,
            (SELECT SUM(x.amount) FROM retail.payments x WHERE x.order_id=p.id AND x.other_kind=p.kind) AS total
            FROM retail.customer_orders p"""
        self.assertIsNone(self.candidate(sql))
