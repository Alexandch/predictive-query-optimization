import unittest
from unittest.mock import Mock
from pqo.dqn_features import collect_action_database_context
from pqo.index_actions import IndexAction


class IndexOverlapTests(unittest.TestCase):
    def context(self, rows, include=(), keys=("a",)):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (42, 100, 2048, 10, 2, 1)
        connection.execute.return_value.fetchall.return_value = rows
        return collect_action_database_context(IndexAction.create("retail", "orders", keys, include), connection=connection)

    def test_plain_keys_do_not_replace_requested_include(self):
        result = self.context([(1, ["a"], True)], include=("b",))
        self.assertFalse(result["exact_index_exists"])
        self.assertFalse(result["prefix_index_exists"])
        self.assertEqual(result["key_overlap_index_count"], 1)

    def test_include_or_trailing_key_can_cover_payload(self):
        for row in ((1, ["a", "b"], True), (2, ["a", "b"], True)):
            result = self.context([row], include=("b",))
            self.assertTrue(result["prefix_index_exists"])
            self.assertEqual(result["exact_index_exists"], row[0] == 1)

    def test_ineligible_indexes_count_but_do_not_suppress_trial(self):
        result = self.context([(1, ["a", "b"], False)], include=("b",))
        self.assertEqual(result["target_index_count"], 1)
        self.assertEqual(result["key_overlap_index_count"], 0)
        self.assertFalse(result["prefix_index_exists"])

    def test_expression_position_is_not_removed(self):
        result = self.context([(2, [None, "a"], False), (2, ["b", "a"], True)])
        self.assertFalse(result["prefix_index_exists"])

    def test_missing_one_payload_column_is_not_full_coverage(self):
        result = self.context([(1, ["a", "b"], True)], include=("b", "c"))
        self.assertFalse(result["exact_index_exists"])
