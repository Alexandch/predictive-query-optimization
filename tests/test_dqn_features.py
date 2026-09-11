import unittest
from types import SimpleNamespace

from pqo.dqn_features import (
    ACTION_FEATURE_NAMES,
    ACTION_V3_FEATURE_NAMES,
    COLUMN_HASH_BUCKETS,
    GENERIC_ACTION_ENCODING,
    GENERIC_V3_ACTION_ENCODING,
    LEGACY_ACTION_ENCODING,
    SEQUENTIAL_ACTION_ENCODING,
    SEQUENTIAL_ACTION_FEATURE_NAMES,
    SEQUENTIAL_STATE_FEATURE_NAMES,
    encode_action,
    encode_sequential_state,
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

    def test_generic_encoding_represents_non_aviation_table(self):
        action = IndexAction.create("retail", "products", ("category_id",))
        generic = encode_action(
            action,
            encoding_version=GENERIC_ACTION_ENCODING,
        )
        legacy = encode_action(
            action,
            encoding_version=LEGACY_ACTION_ENCODING,
        )

        self.assertEqual(sum(generic[6:14]), 1.0)
        self.assertEqual(generic[14], 0.0)
        self.assertEqual(legacy[14], 1.0)
        self.assertNotEqual(generic, legacy)

    def test_v3_marks_non_sargable_keys_and_adds_table_context(self):
        action = IndexAction.create("retail", "products", ("price",))
        direct = encode_action(
            action,
            "SELECT * FROM retail.products WHERE price >= 100",
            encoding_version=GENERIC_V3_ACTION_ENCODING,
            database_context={"target_relation_rows": 1000},
        )
        expression = encode_action(
            action,
            "SELECT * FROM retail.products WHERE ROUND(price) >= 100",
            encoding_version=GENERIC_V3_ACTION_ENCODING,
            database_context={
                "target_relation_rows": 1000,
                "target_relation_size_bytes": 8192,
                "target_index_count": 3,
                "exact_index_exists": True,
                "prefix_index_exists": True,
            },
        )

        self.assertEqual(len(expression), len(ACTION_V3_FEATURE_NAMES))
        self.assertEqual(len(expression), len(ACTION_FEATURE_NAMES) + 7)
        self.assertEqual(direct[-2:], [0.0, 0.0])
        self.assertEqual(expression[-2:], [1.0, 1.0])
        self.assertEqual(expression[-4:-2], [1.0, 1.0])

    def test_sequential_encoding_distinguishes_stop(self):
        stop = encode_action(
            IndexAction.stop(),
            encoding_version=SEQUENTIAL_ACTION_ENCODING,
        )
        create = encode_action(
            IndexAction.create("aviation", "flights", ("status",)),
            "SELECT * FROM aviation.flights WHERE status = 'Scheduled'",
            encoding_version=SEQUENTIAL_ACTION_ENCODING,
        )

        self.assertEqual(len(stop), len(SEQUENTIAL_ACTION_FEATURE_NAMES))
        self.assertEqual(stop[-1], 1.0)
        self.assertEqual(create[-1], 0.0)

    def test_sequential_state_includes_budget_and_selected_indexes(self):
        state = SimpleNamespace(
            max_steps=3,
            storage_budget_bytes=1000,
            remaining_budget_bytes=750,
            baseline_time_ms=100.0,
            current_time_ms=60.0,
            cumulative_improvement_ratio=0.4,
            step=1,
            selected_actions=(
                IndexAction.create("aviation", "flights", ("status",)),
            ),
        )
        encoded = encode_sequential_state([1.0, 2.0], state)

        self.assertEqual(len(encoded), 2 + len(SEQUENTIAL_STATE_FEATURE_NAMES))
        self.assertEqual(encoded[2], 1 / 3)
        self.assertEqual(encoded[3], 0.75)
        self.assertEqual(encoded[5], 0.6)


if __name__ == "__main__":
    unittest.main()
