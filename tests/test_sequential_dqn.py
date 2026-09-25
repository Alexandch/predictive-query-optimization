import json
from pathlib import Path
import tempfile
import unittest

from pqo.dqn_features import SEQUENTIAL_ACTION_ENCODING
from pqo.sequential_dqn import (
    _build_network,
    _bellman_returns,
    _control_decision_rows,
    _episode_parameter_split,
    merge_sequential_experience,
    predict_sequential_action_values,
    sequential_inference_metadata,
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
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def test_numpy_runtime_matches_pytorch_artifact(self):
        import numpy as np
        import torch

        model_path = (
            self.PROJECT_ROOT
            / "models/sequential_dqn/sequential_dqn_index_advisor.pt"
        )
        artifact = torch.load(model_path, map_location="cpu", weights_only=True)
        state = np.linspace(-0.5, 0.5, artifact["state_size"]).tolist()
        actions = [
            np.linspace(0.25, -0.25, artifact["action_size"]).tolist(),
            np.linspace(-0.1, 0.1, artifact["action_size"]).tolist(),
        ]
        actions[0][-1] = 0.0
        actions[1][-1] = 1.0

        actual = predict_sequential_action_values(model_path, state, actions)
        model = _build_network(torch.nn, artifact["input_size"])
        model.load_state_dict(artifact["model_state_dict"])
        model.eval()
        combined = np.asarray([state + action for action in actions], dtype=np.float32)
        normalized = (
            combined - artifact["feature_mean"].numpy()
        ) / artifact["feature_std"].numpy()
        with torch.no_grad():
            expected = model(torch.tensor(normalized)).squeeze(1).numpy()

        self.assertAlmostEqual(actual[0], float(expected[0]), places=5)
        self.assertEqual(actual[1], 0.0)
        metadata = sequential_inference_metadata(model_path)
        self.assertEqual(metadata["action_encoding_version"], SEQUENTIAL_ACTION_ENCODING)

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

    def test_control_threshold_keeps_stop_when_create_q_is_too_low(self):
        import numpy as np

        records = [
            record("e", "s", "stop", 0.0),
            record("e", "s", "create", 0.3),
        ]
        rows = _control_decision_rows(
            records,
            np.asarray([0.0, 0.2]),
            np.asarray([0.0, 0.3]),
            0.25,
            np,
        )

        self.assertEqual(json.loads(rows[0]["predicted_action"])["kind"], "stop")
        self.assertAlmostEqual(rows[0]["regret"], 0.3)


if __name__ == "__main__":
    unittest.main()
