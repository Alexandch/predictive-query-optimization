import os
import unittest
from unittest.mock import patch

from pqo.config import DatabaseSettings


class DatabaseSettingsTests(unittest.TestCase):
    def test_default_allowlist_contains_all_benchmark_schemas(self):
        settings = DatabaseSettings()

        self.assertEqual(
            settings.allowed_schemas,
            frozenset({"aviation", "retail", "logistics", "chbenchmark", "pagila"}),
        )

    def test_reads_comma_separated_schema_allowlist(self):
        with patch.dict(
            os.environ,
            {"PQO_ALLOWED_SCHEMAS": "aviation, retail"},
            clear=False,
        ):
            settings = DatabaseSettings.from_env()

        self.assertEqual(settings.allowed_schemas, frozenset({"aviation", "retail"}))

    def test_rejects_unsafe_schema_name(self):
        with self.assertRaises(ValueError):
            DatabaseSettings(allowed_schemas=frozenset({"retail;drop"}))


if __name__ == "__main__":
    unittest.main()
