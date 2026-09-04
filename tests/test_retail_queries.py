import unittest

from pqo.explain import _assert_read_only_query
from pqo.index_actions import IndexActionKind, generate_index_actions
from pqo.retail_queries import RetailQueryGenerator
from pqo.retail_control_queries import RetailControlQueryGenerator


class RetailQueryGeneratorTests(unittest.TestCase):
    def test_generates_every_training_template(self):
        generator = RetailQueryGenerator(seed=1)
        cases = generator.generate_one_per_template()

        self.assertEqual(len(cases), 18)
        self.assertEqual({case.template_id for case in cases}, set(generator.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)

    def test_retail_allowlist_produces_index_candidates(self):
        cases = RetailQueryGenerator(seed=2).generate_one_per_template()
        candidate_count = sum(
            any(
                action.kind is IndexActionKind.CREATE
                for action in generate_index_actions(
                    case.sql_text,
                    allowed_schemas=frozenset({"retail"}),
                )
            )
            for case in cases
        )

        self.assertGreaterEqual(candidate_count, 15)

    def test_control_templates_are_sealed_and_valid(self):
        training = RetailQueryGenerator(seed=1)
        control = RetailControlQueryGenerator(seed=1)
        cases = control.generate_one_per_template()

        self.assertEqual(len(cases), 15)
        self.assertTrue(set(training.template_ids).isdisjoint(control.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)


if __name__ == "__main__":
    unittest.main()
