import unittest

from pqo.sequential_recommendation import (
    _is_statement_timeout,
    _should_measure_candidate,
)


class SequentialRecommendationTests(unittest.TestCase):
    def test_deep_analysis_verifies_one_candidate_below_model_threshold(self):
        self.assertTrue(_should_measure_candidate(0.1, 0.5, 0))
        self.assertFalse(_should_measure_candidate(0.1, 0.5, 1))
        self.assertTrue(_should_measure_candidate(0.6, 0.5, 1))

    def test_recognizes_postgresql_statement_timeout(self):
        error = RuntimeError("canceling statement due to statement timeout")
        error.sqlstate = "57014"

        self.assertTrue(_is_statement_timeout(error))
        self.assertFalse(_is_statement_timeout(RuntimeError("other")))


if __name__ == "__main__":
    unittest.main()
