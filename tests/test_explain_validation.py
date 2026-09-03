import unittest

from pqo.explain import _assert_read_only_query


class ExplainValidationTests(unittest.TestCase):
    def test_accepts_select_and_with_select(self):
        self.assertEqual(_assert_read_only_query("SELECT 1"), "SELECT 1")
        self.assertTrue(
            _assert_read_only_query("WITH x AS (SELECT 1) SELECT * FROM x")
        )

    def test_rejects_data_modifying_cte(self):
        with self.assertRaises(ValueError):
            _assert_read_only_query(
                "WITH deleted AS (DELETE FROM aviation.flights RETURNING *) "
                "SELECT * FROM deleted"
            )

    def test_rejects_multiple_statements(self):
        with self.assertRaises(ValueError):
            _assert_read_only_query("SELECT 1; SELECT 2")


if __name__ == "__main__":
    unittest.main()
