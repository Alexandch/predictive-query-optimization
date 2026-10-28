import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "prediction_audit", Path(__file__).resolve().parents[1] / "scripts/audit_first_prediction.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class FirstPredictionAuditTests(unittest.TestCase):
    def test_macro_weights_families_not_queries(self):
        result = audit.summarize([
            {"template": "a", "actual_ms": 10, "predicted_ms": 10},
            {"template": "a", "actual_ms": 20, "predicted_ms": 20},
            {"template": "b", "actual_ms": 10, "predicted_ms": 40},
        ])
        self.assertEqual(result["macro_family_mae_ms"], 15)
        self.assertEqual(result["over_factor_two"], 1)
        self.assertEqual(result["worst_absolute_errors"][0]["direction"], "over")

    def test_rejects_missing_or_invalid_times(self):
        for points in ([], [{"template": "a", "actual_ms": 0, "predicted_ms": 10}],
                       [{"template": "a", "actual_ms": 1, "predicted_ms": float("nan")} ]):
            with self.assertRaises(ValueError):
                audit.summarize(points)
