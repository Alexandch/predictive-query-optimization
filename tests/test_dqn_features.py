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
            len(vector), 6 + 9 + 2 * COLUMN_HASH_BUCKETS
        )


if __name__ == "__main__":
    unittest.main()
