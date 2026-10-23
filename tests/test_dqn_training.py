import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from pqo.dqn import (
    dqn_action_encoding_version,
    export_dqn_numpy_artifact,
    predict_action_values,
    train_dqn,
)
from pqo.dqn_features import (
    ACTION_FEATURE_NAMES,
    LEGACY_ACTION_ENCODING,
    STATE_FEATURE_NAMES,
)


class DQNTrainingTests(unittest.TestCase):
    def test_packaged_dqn_inference_never_imports_torch(self):
        code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "torch" or name.startswith("torch."):
        raise AssertionError("Runtime DQN must not import PyTorch")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from pqo.dqn import _numpy_artifact, dqn_action_encoding_version, predict_action_values
path = "models/dqn/dqn_index_advisor.pt"
artifact = _numpy_artifact(path)
size = artifact["feature_mean"].size
assert dqn_action_encoding_version(path)
assert len(predict_action_values(path, [0.] * (size - 2), [[0., 1.]])) == 1
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_numpy_inference_matches_torch_network(self):
        import numpy as np
        import torch
        from pqo.dqn import _build_network

        torch.manual_seed(41)
        state_size, action_size = 7, 5
        size = state_size + action_size
        network = _build_network(torch.nn, size).eval()
        mean = torch.linspace(-2, 2, size)
        std = torch.linspace(0.2, 3, size)
        generator = np.random.default_rng(41)
        state = generator.normal(size=state_size).tolist()
        actions = generator.normal(size=(37, action_size)).tolist()
        inputs = np.asarray([state + action for action in actions], dtype=np.float32)
        with torch.no_grad():
            expected = network((torch.tensor(inputs) - mean) / std).numpy()[:, 0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.pt"
            torch.save({
                "input_size": size,
                "model_state_dict": network.state_dict(),
                "feature_mean": mean,
                "feature_std": std,
            }, path)
            export_dqn_numpy_artifact(path)
            actual = predict_action_values(path, state, actions)
        np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-6)
        self.assertEqual(int(np.argmax(actual)), int(np.argmax(expected)))

    def test_trains_saves_and_loads_model(self):
        state_size = len(STATE_FEATURE_NAMES)
        action_size = len(ACTION_FEATURE_NAMES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experience = root / "experience.jsonl"
            records = []
            for template in range(4):
                for query in range(5):
                    for action in range(2):
                        records.append(
                            {
                                "template_id": f"template_{template}",
                                "query_id": f"query_{template}_{query}",
                                "state": [float(template)] * state_size,
                                "action_features": [float(action)] * action_size,
                                "action": {
                                    "kind": "noop" if action == 0 else "create"
                                },
                                "reward": float(action),
                            }
                        )
            experience.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            metrics = train_dqn(
                experience,
                root / "model",
                epochs=5,
                sampling_mode="template-balanced",
            )
            values = predict_action_values(
                root / "model" / "dqn_index_advisor.pt",
                [0.0] * state_size,
                [[0.0] * action_size, [1.0] * action_size],
            )

            self.assertEqual(metrics.experience_count, 40)
            self.assertEqual(metrics.action_encoding_version, LEGACY_ACTION_ENCODING)
            self.assertEqual(metrics.sampling_mode, "template-balanced")
            self.assertEqual(
                dqn_action_encoding_version(
                    root / "model" / "dqn_index_advisor.pt"
                ),
                LEGACY_ACTION_ENCODING,
            )
            self.assertEqual(len(values), 2)
            self.assertTrue((root / "model" / "metrics.json").exists())


if __name__ == "__main__":
    unittest.main()
