import unittest

from pqo.explain import _assert_read_only_query
from pqo.index_actions import IndexActionKind, generate_index_actions
from pqo.pagila_queries import PagilaQueryGenerator


class PagilaQueryTests(unittest.TestCase):
    def test_templates_are_balanced_unique_read_only_and_actionable(self):
        generator = PagilaQueryGenerator(seed=1)
        cases = generator.generate_one_per_template()

        self.assertEqual(len(cases), 30)
        self.assertEqual(len(set(generator.template_ids)), 30)
        self.assertEqual({case.template_id for case in cases}, set(generator.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)
            self.assertIn("pagila.", case.sql_text.lower())
            actions = generate_index_actions(
                case.sql_text,
                allowed_schemas=frozenset({"pagila"}),
            )
            self.assertTrue(any(action.kind is IndexActionKind.CREATE for action in actions))

    def test_generation_is_deterministic_and_rejects_empty_count(self):
        first = PagilaQueryGenerator(seed=10).generate(60)
        second = PagilaQueryGenerator(seed=10).generate(60)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            PagilaQueryGenerator().generate(0)


if __name__ == "__main__":
    unittest.main()
