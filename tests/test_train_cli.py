from dataclasses import dataclass
import io
import unittest
from unittest.mock import patch

from pqo.train_cli import main


@dataclass
class StubMetrics:
    sample_count: int = 100


class TrainCLITests(unittest.TestCase):
    def test_sql_v3_is_forwarded_to_training(self):
        with patch("sys.argv", ["pqo-train", "dataset.csv", "candidate", "--feature-version", "sql-v3", "--no-tune"]), \
             patch("pqo.train_cli.train_xgboost", return_value=StubMetrics()) as train, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        self.assertEqual(train.call_args.kwargs["feature_version"], "sql-v3")
        self.assertFalse(train.call_args.kwargs["tune"])
