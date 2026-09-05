import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pqo.app_paths import resource_root, writable_root


class AppPathTests(unittest.TestCase):
    def test_source_resource_root_contains_models(self):
        self.assertTrue((resource_root() / "models").is_dir())

    def test_explicit_writable_root_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PQO_APP_DATA_ROOT": directory}
        ):
            self.assertEqual(writable_root(), Path(directory).resolve())

    def test_frozen_resource_root_uses_meipass(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sys, "frozen", True, create=True
        ), patch.object(sys, "_MEIPASS", directory, create=True):
            self.assertEqual(resource_root(), Path(directory).resolve())


if __name__ == "__main__":
    unittest.main()
