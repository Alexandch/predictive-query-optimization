import unittest

from pqo.query_generator import AviationQueryGenerator
from pqo.sql_features import extract_sql_features


class AviationQueryGeneratorTests(unittest.TestCase):
    def test_generation_is_reproducible(self):
        first = AviationQueryGenerator(seed=123).generate(20)
        second = AviationQueryGenerator(seed=123).generate(20)

        self.assertEqual(first, second)

    def test_generates_valid_queries_from_all_templates(self):
        generator = AviationQueryGenerator(seed=7)
        cases = generator.generate_one_per_template()

        self.assertEqual(
            {case.template_id for case in cases},
            set(generator.template_ids),
        )
        for case in cases:
            features = extract_sql_features(case.sql_text)
            self.assertGreater(features.query_length, 0)

    def test_rejects_non_positive_count(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            AviationQueryGenerator().generate(0)

    def test_template_distribution_is_balanced(self):
        cases = AviationQueryGenerator(seed=7).generate(50)
        counts = {
            template_id: sum(case.template_id == template_id for case in cases)
            for template_id in AviationQueryGenerator.TEMPLATE_IDS
        }

        self.assertEqual(set(counts.values()), {2, 3})


if __name__ == "__main__":
    unittest.main()
