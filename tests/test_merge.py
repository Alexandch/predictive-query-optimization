import csv
import json
from pathlib import Path
import tempfile
import unittest

from pqo.dqn_features import GENERIC_ACTION_ENCODING
from pqo.merge import merge_datasets, merge_dqn_experience


class DatasetMergeTests(unittest.TestCase):
    def test_merges_compatible_csv_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.csv"
            second = root / "second.csv"
            output = root / "merged.csv"
            first.write_text("a,b\n1,2\n", encoding="utf-8")
            second.write_text("a,b\n3,4\n", encoding="utf-8")

            count = merge_datasets([first, second], output)

            with output.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(count, 2)
            self.assertEqual(rows, [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])

    def test_rejects_different_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.csv"
            second = root / "second.csv"
            first.write_text("a\n1\n", encoding="utf-8")
            second.write_text("b\n2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                merge_datasets([first, second], root / "output.csv")

    def test_merges_and_reencodes_dqn_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = [root / "one.jsonl", root / "two.jsonl"]
            records = [
                {
                    "template_id": "one",
                    "query_id": "q1",
                    "sql_text": "SELECT * FROM retail.products WHERE category_id = 1",
                    "state": [0.0],
                    "action": {
                        "kind": "create",
                        "schema_name": "retail",
                        "table_name": "products",
                        "key_columns": ["category_id"],
                        "include_columns": [],
                    },
                    "action_features": [99.0],
                    "reward": 0.2,
                },
                {
                    "template_id": "two",
                    "query_id": "q2",
                    "sql_text": "SELECT 1",
                    "state": [0.0],
                    "action": {
                        "kind": "noop",
                        "schema_name": None,
                        "table_name": None,
                        "key_columns": [],
                        "include_columns": [],
                    },
                    "action_features": [99.0],
                    "reward": 0.0,
                },
            ]
            for path, record in zip(inputs, records, strict=True):
                path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            output = root / "merged.jsonl"

            count = merge_dqn_experience(inputs, output)
            merged = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(count, 2)
            self.assertTrue(all(len(record["action_features"]) > 1 for record in merged))
            self.assertTrue(
                all(
                    record["action_encoding_version"] == GENERIC_ACTION_ENCODING
                    for record in merged
                )
            )


if __name__ == "__main__":
    unittest.main()
