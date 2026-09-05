import unittest

from pqo.explain import _assert_read_only_query
from pqo.index_actions import IndexActionKind, generate_index_actions
from pqo.logistics_control_queries import LogisticsControlQueryGenerator
from pqo.production_queries import ProductionQueryGenerator
from pqo.retail_control_queries import RetailControlQueryGenerator
from pqo.retail_queries import RetailQueryGenerator


class LogisticsControlQueryTests(unittest.TestCase):
    def test_templates_are_isolated_and_read_only(self):
        generator = LogisticsControlQueryGenerator(seed=1)
        cases = generator.generate_one_per_template()
        known = {
            *ProductionQueryGenerator.TEMPLATE_IDS,
            *RetailQueryGenerator.TEMPLATE_IDS,
            *RetailControlQueryGenerator.TEMPLATE_IDS,
        }

        self.assertEqual(len(cases), 15)
        self.assertTrue(known.isdisjoint(generator.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)

    def test_allowlist_generates_logistics_candidates(self):
        cases = LogisticsControlQueryGenerator(seed=2).generate_one_per_template()
        covered = sum(
            any(
                action.kind is IndexActionKind.CREATE
                for action in generate_index_actions(
                    case.sql_text,
                    allowed_schemas=frozenset({"logistics"}),
                )
            )
            for case in cases
        )
        self.assertGreaterEqual(covered, 12)


if __name__ == "__main__":
    unittest.main()
