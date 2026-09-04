import unittest

from pqo.explain import _assert_read_only_query
from pqo.production_queries import ProductionQueryGenerator
from pqo.query_generator import AviationQueryGenerator


class ProductionQueryGeneratorTests(unittest.TestCase):
    def test_has_thirty_templates_disjoint_from_training(self):
        generator = ProductionQueryGenerator(seed=1)
        self.assertEqual(len(generator.template_ids), 30)
        self.assertEqual(len(set(generator.template_ids)), 30)
        self.assertTrue(
            set(generator.template_ids).isdisjoint(AviationQueryGenerator.TEMPLATE_IDS)
        )

    def test_every_template_is_parseable_read_only_sql(self):
        cases = ProductionQueryGenerator(seed=2).generate_one_per_template()
        self.assertEqual({case.template_id for case in cases}, set(ProductionQueryGenerator.TEMPLATE_IDS))
        for case in cases:
            with self.subTest(template=case.template_id):
                self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)

    def test_generation_is_deterministic(self):
        first = ProductionQueryGenerator(seed=7).generate(75)
        second = ProductionQueryGenerator(seed=7).generate(75)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len({case.sql_text for case in first}), 60)


if __name__ == "__main__":
    unittest.main()
