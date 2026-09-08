import json
import tempfile
import unittest
from pathlib import Path

from pqo.training_baseline import create_baseline, file_sha256, verify_baseline


class TrainingBaselineTests(unittest.TestCase):
    def test_create_and_verify_detects_later_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = root / "control.txt"
            tracked.write_text("sealed", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest = create_baseline(
                root,
                manifest_path,
                files=("control.txt",),
            )

            self.assertEqual(manifest["files"]["control.txt"]["sha256"], file_sha256(tracked))
            self.assertEqual(verify_baseline(root, manifest_path), [])
            tracked.write_text("changed", encoding="utf-8")
            self.assertEqual(
                verify_baseline(root, manifest_path),
                ["hash mismatch: control.txt"],
            )

    def test_manifest_uses_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text("value", encoding="utf-8")
            destination = root / "nested" / "manifest.json"
            create_baseline(root, destination, files=("sample.txt",))
            data = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(list(data["files"]), ["sample.txt"])


if __name__ == "__main__":
    unittest.main()
