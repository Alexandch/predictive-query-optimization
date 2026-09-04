import unittest

from pqo.history import load_analysis_history


class HistoryTests(unittest.TestCase):
    def test_rejects_invalid_limit(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 10000"):
            load_analysis_history(limit=0)


if __name__ == "__main__":
    unittest.main()
