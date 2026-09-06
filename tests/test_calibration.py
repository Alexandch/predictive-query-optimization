import math
from pathlib import Path
import tempfile
import unittest

from pqo.calibration import (
    add_observation,
    apply_calibration,
    load_profile,
    new_profile,
    save_profile,
    suggested_profile_path,
    validate_profile,
)
from pqo.config import DatabaseSettings


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.settings = DatabaseSettings(
            dbname="customer_db",
            host="db.example",
            port=5432,
            allowed_schemas=frozenset({"public"}),
        )

    def test_ten_unique_queries_activate_robust_factor(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for number in range(1, 11):
                profile, _ = add_observation(
                    profile,
                    f"SELECT {number}",
                    100.0 + number,
                    200.0 + 2 * number,
                )

            self.assertTrue(profile.ready)
            self.assertEqual(profile.unique_query_count, 10)
            self.assertTrue(math.isclose(profile.factor, 2.0, rel_tol=0.02))
            self.assertTrue(
                math.isclose(apply_calibration(100.0, profile), 200.0, rel_tol=0.02)
            )

    def test_repeated_query_does_not_activate_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for _ in range(10):
                profile, _ = add_observation(profile, "SELECT 1", 10.0, 20.0)

            self.assertFalse(profile.ready)
            self.assertEqual(apply_calibration(10.0, profile), 10.0)

    def test_round_trip_and_compatibility_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.joblib"
            other_model = root / "other.joblib"
            model.write_bytes(b"model-a")
            other_model.write_bytes(b"model-b")
            profile = new_profile(self.settings, model)
            profile, _ = add_observation(profile, "SELECT 1", 10.0, 15.0)
            path = suggested_profile_path(root, self.settings, model)
            save_profile(profile, path)

            loaded = load_profile(path)
            self.assertEqual(loaded, profile)
            validate_profile(loaded, self.settings, model)
            with self.assertRaisesRegex(ValueError, "another XGBoost model"):
                validate_profile(loaded, self.settings, other_model)


if __name__ == "__main__":
    unittest.main()
