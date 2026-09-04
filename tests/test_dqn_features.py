import unittest

from pqo.dqn_features import (
    ACTION_FEATURE_NAMES,
    COLUMN_HASH_BUCKETS,
    encode_action,
)
from pqo.index_actions import IndexAction


class DQNFeatureTests(unittest.TestCase):
    def test_action_vector_has_stable_size(self):
        noop = encode_action(IndexAction.noop())
        create = encode_action(
            IndexAction.create(
                "aviation",
                "flights",
                ("departure_airport", "status"),
                ("flight_id",),
            )
        )
        self.assertEqual(len(noop), len(ACTION_FEATURE_NAMES))
        self.assertEqual(len(create), len(ACTION_FEATURE_NAMES))
        self.assertNotEqual(noop, create)

    def test_column_hash_features_are_bounded(self):
        vector = encode_action(
            IndexAction.create("aviation", "flights", ("status",))
        )
        self.assertEqual(
            len(vector), 6 + 9 + 2 * COLUMN_HASH_BUCKETS + 8
        )

    def test_action_context_distinguishes_predicate_and_order_columns(self):
        action = IndexAction.create(
            "aviation", "flights", ("departure_airport", "scheduled_departure")
        )
        vector = encode_action(
            action,
            "SELECT flight_id FROM aviation.flights AS f "
            "WHERE f.departure_airport = 'MSQ' ORDER BY f.scheduled_departure",
        )
        context = vector[-8:]

        self.assertEqual(context[0], 1.0)
        self.assertEqual(context[2], 1.0)
        self.assertEqual(context[4], 1.0)
        self.assertEqual(context[7], 1.0)


if __name__ == "__main__":
    unittest.main()
