import json
import math
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from pqo.control_benchmark import evaluate_dqn_control, evaluate_xgboost_control


class ControlBenchmarkTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_evaluates_xgboost_and_writes_predictions(self):
        source = pd.read_csv(
            self.ROOT / "dataset/postgresql/aviation_dataset.csv", nrows=240
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            dataset = root / "control.csv"
            source.to_csv(dataset, index=False)
            metrics = evaluate_xgboost_control(
                dataset,
                self.ROOT / "models/xgboost/xgboost_query_time.joblib",
                root / "result",
            )
            self.assertTrue(math.isfinite(metrics.mae_ms))
            self.assertGreater(metrics.unique_query_count, 0)
            self.assertTrue((root / "result/xgboost_control_metrics.json").is_file())
            self.assertTrue((root / "result/xgboost_control_predictions.csv").is_file())
            self.assertTrue((root / "result/xgboost_control_by_template.csv").is_file())

    def test_evaluates_dqn_holdout_actions(self):
        source = self.ROOT / "dataset/postgresql/dqn_experience.jsonl"
        grouped = {}
        with source.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                grouped.setdefault(record["query_id"], []).append(record)
                if len(grouped) >= 3 and all(len(items) >= 2 for items in grouped.values()):
                    break
        records = [record for items in grouped.values() for record in items]
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            experience = root / "control.jsonl"
            experience.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            metrics = evaluate_dqn_control(
                experience,
                self.ROOT / "models/dqn/dqn_index_advisor.pt",
                root / "result",
            )
            self.assertGreaterEqual(metrics.query_count, 1)
            self.assertGreaterEqual(metrics.action_count, metrics.query_count * 2)
            self.assertTrue((root / "result/dqn_control_metrics.json").is_file())
            self.assertTrue((root / "result/dqn_control_decisions.csv").is_file())


if __name__ == "__main__":
    unittest.main()
