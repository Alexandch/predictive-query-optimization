import csv
from pathlib import Path
import tempfile
import unittest

from pqo.merge import merge_datasets


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


if __name__ == "__main__":
    unittest.main()
