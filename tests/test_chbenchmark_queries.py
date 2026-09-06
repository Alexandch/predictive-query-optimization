import unittest

from pqo.chbenchmark_control_queries import CHBenchmarkControlQueryGenerator
from pqo.chbenchmark_queries import CHBenchmarkQueryGenerator
from pqo.explain import _assert_read_only_query
from pqo.index_actions import IndexActionKind, generate_index_actions


class CHBenchmarkQueryGeneratorTests(unittest.TestCase):
    def test_training_templates_are_complete_and_read_only(self):
        generator = CHBenchmarkQueryGenerator(seed=1)
        cases = generator.generate_one_per_template()

        self.assertEqual(len(cases), 20)
        self.assertEqual({case.template_id for case in cases}, set(generator.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)

    def test_control_templates_are_sealed_and_read_only(self):
        training = CHBenchmarkQueryGenerator(seed=1)
        control = CHBenchmarkControlQueryGenerator(seed=1)
        cases = control.generate_one_per_template()

        self.assertEqual(len(cases), 10)
        self.assertTrue(set(training.template_ids).isdisjoint(control.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)

    def test_training_and_control_queries_offer_index_candidates(self):
        cases = [
            *CHBenchmarkQueryGenerator(seed=2).generate_one_per_template(),
            *CHBenchmarkControlQueryGenerator(seed=2).generate_one_per_template(),
        ]
        covered = sum(
            any(
                action.kind is IndexActionKind.CREATE
                for action in generate_index_actions(
                    case.sql_text,
                    allowed_schemas=frozenset({"chbenchmark"}),
                )
            )
            for case in cases
        )
        self.assertGreaterEqual(covered, 26)


if __name__ == "__main__":
    unittest.main()
