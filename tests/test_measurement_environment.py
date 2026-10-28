import json
import unittest
from unittest.mock import MagicMock, patch

from pqo.config import DatabaseSettings
from pqo.measurement_environment import capture_environment


class MeasurementEnvironmentTests(unittest.TestCase):
    def test_read_only_environment_snapshot_excludes_credentials(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.side_effect = [
            [("work_mem", "4096", "kB"), ("jit", "on", None)],
            [("retail", "products", 1000.0, 20)],
        ]
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            snapshot = capture_environment(DatabaseSettings(password="not-for-artifacts"))
        self.assertEqual(connection.execute.call_args_list[0].args[0], "SET TRANSACTION READ ONLY")
        self.assertEqual(snapshot["settings"]["work_mem"]["value"], "4096")
        self.assertEqual(snapshot["measurement"]["unit"], "ms")
        self.assertEqual(snapshot["measurement"]["warmups"], 1)
        self.assertEqual(snapshot["measurement"]["repetitions"], 3)
        self.assertEqual(snapshot["relation_estimates"][0]["table"], "products")
        self.assertNotIn("not-for-artifacts", json.dumps(snapshot))
