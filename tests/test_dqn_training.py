import json
from pathlib import Path
import tempfile
import unittest

from pqo.dqn import (
    dqn_action_encoding_version,
    predict_action_values,
    train_dqn,
)
from pqo.dqn_features import (
    ACTION_FEATURE_NAMES,
    LEGACY_ACTION_ENCODING,
    STATE_FEATURE_NAMES,
)


class DQNTrainingTests(unittest.TestCase):
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

            metrics = train_dqn(experience, root / "model", epochs=5)
            values = predict_action_values(
                root / "model" / "dqn_index_advisor.pt",
                [0.0] * state_size,
                [[0.0] * action_size, [1.0] * action_size],
            )

            self.assertEqual(metrics.experience_count, 40)
            self.assertEqual(metrics.action_encoding_version, LEGACY_ACTION_ENCODING)
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
