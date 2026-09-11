import json
from pathlib import Path
import tempfile
import unittest

from pqo.dqn_features import SEQUENTIAL_ACTION_ENCODING
from pqo.sequential_dqn import (
    _bellman_returns,
    _episode_parameter_split,
    merge_sequential_experience,
)


def record(episode, state, action, reward, *, next_state=None, template="t"):
    return {
        "format_version": 1,
        "action_encoding_version": SEQUENTIAL_ACTION_ENCODING,
        "episode_id": episode,
        "state_id": state,
        "template_id": template,
        "state": [0.0, 1.0],
        "action": {"kind": action},
        "action_features": [1.0, 0.0],
        "reward": reward,
        "done": next_state is None,
        "next_state_id": next_state,
    }


class SequentialDQNTests(unittest.TestCase):
    def test_bellman_return_uses_best_measured_next_action(self):
        records = [
            record("e", "s0", "stop", 0.0),
            record("e", "s0", "create", 0.2, next_state="s1"),
            record("e", "s1", "stop", 0.0),
            record("e", "s1", "create", 0.4),
        ]

        self.assertEqual(_bellman_returns(records, 0.5), [0.0, 0.4, 0.0, 0.4])

    def test_episode_split_never_splits_one_trajectory(self):
        records = []
        for number in range(4):
            records.extend(
                [
                    record(f"e{number}", f"s{number}", "stop", 0.0),
                    record(f"e{number}", f"s{number}", "create", 0.1),
                ]
            )
        import random

        partitions = _episode_parameter_split(records, random.Random(3))
        episode_sets = [{row["episode_id"] for row in part} for part in partitions]
        self.assertFalse(episode_sets[0] & episode_sets[1])
        self.assertFalse(episode_sets[0] & episode_sets[2])
        self.assertFalse(episode_sets[1] & episode_sets[2])

    def test_merge_rejects_a_missing_next_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jsonl"
            output = Path(directory) / "output.jsonl"
            source.write_text(
                json.dumps(record("e", "s0", "create", 0.1, next_state="missing"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Missing next state"):
                merge_sequential_experience([source], output)


if __name__ == "__main__":
    unittest.main()
