import json
import math
from pathlib import Path
import tempfile
import unittest

from pqo.calibration import (
    add_observation,
    apply_calibration,
    load_profile,
    new_profile,
    parse_calibration_workload,
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

    def test_profile_is_not_applied_when_it_worsens_mae(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for number in range(9):
                profile, _ = add_observation(
                    profile, f"SELECT {number}", 100.0, 100.0
                )
            profile, _ = add_observation(profile, "SELECT 99", 10_000.0, 20_000.0)

            self.assertTrue(profile.ready)
            self.assertFalse(profile.improves_mae)
            self.assertEqual(apply_calibration(100.0, profile, "SELECT 500"), 100.0)

    def test_parses_semicolon_workload_and_rejects_writes(self):
        queries = parse_calibration_workload(
            "SELECT 1; SELECT ';' AS separator; SELECT 1;"
        )
        self.assertEqual(len(queries), 2)
        with self.assertRaises(ValueError):
            parse_calibration_workload("SELECT 1; DELETE FROM public.orders;")

    def test_conditional_factor_requires_three_queries_in_same_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for number in range(10):
                profile, _ = add_observation(
                    profile,
                    f"SELECT {number} WHERE {number} >= 0",
                    10.0,
                    20.0,
                )

            self.assertEqual(profile.active_segment_count, 1)
            adjusted = apply_calibration(
                10.0, profile, "SELECT 100 WHERE 100 >= 0"
            )
            self.assertGreater(adjusted, 10.0)
            self.assertLess(adjusted, 20.0)
            self.assertEqual(
                apply_calibration(
                    500.0,
                    profile,
                    "SELECT * FROM a JOIN b ON b.id = a.id",
                ),
                500.0,
            )

    def test_distinct_segments_receive_distinct_shrunk_factors(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for number in range(5):
                profile, _ = add_observation(
                    profile, f"SELECT {number}", 10.0, 20.0
                )
                profile, _ = add_observation(
                    profile,
                    f"SELECT * FROM a JOIN b ON b.id = a.id WHERE a.id = {number}",
                    500.0,
                    250.0,
                )

            fast = apply_calibration(10.0, profile, "SELECT 999")
            joined = apply_calibration(
                500.0,
                profile,
                "SELECT * FROM a JOIN b ON b.id = a.id WHERE a.id = 999",
            )
            self.assertGreater(fast, 10.0)
            self.assertLess(joined, 500.0)
            self.assertEqual(profile.active_segment_count, 2)

    def test_unseen_shape_keeps_the_base_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model-a")
            profile = new_profile(self.settings, model)
            for number in range(10):
                profile, _ = add_observation(
                    profile,
                    f"SELECT {number} WHERE {number} >= 0",
                    10.0,
                    20.0,
                )

            self.assertEqual(
                apply_calibration(
                    10.0,
                    profile,
                    "SELECT 100 WHERE 100 BETWEEN 0 AND 200",
                ),
                10.0,
            )

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

            legacy_data = json.loads(path.read_text(encoding="utf-8"))
            legacy_data["observations"][0].pop("segment")
            legacy_data["observations"][0].pop("shape_hash")
            path.write_text(json.dumps(legacy_data), encoding="utf-8")
            legacy = load_profile(path)
            self.assertEqual(legacy.observations[0].segment, "")
            self.assertEqual(legacy.observations[0].shape_hash, "")


if __name__ == "__main__":
    unittest.main()
