from pathlib import Path
import tempfile
import unittest

from pqo.calibration_evaluation import run_calibration_experiment


class CalibrationEvaluationTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_template_disjoint_experiment_writes_complete_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_calibration_experiment(
                self.ROOT / "dataset/postgresql/production_control.csv",
                self.ROOT / "models/xgboost/xgboost_query_time.joblib",
                directory,
                calibration_fraction=0.20,
                seed=1701,
                split_mode="unseen-template",
            )

            self.assertEqual(result.calibration_template_count, 6)
            self.assertEqual(result.holdout_template_count, 24)
            self.assertEqual(result.sql_overlap_count, 0)
            self.assertGreaterEqual(result.calibration_query_count, 3)
            self.assertGreater(result.holdout_query_count, 0)
            self.assertGreater(result.active_segment_count, 0)
            self.assertTrue(
                Path(directory, "calibration_profile.json").is_file()
            )
            self.assertTrue(
                Path(directory, "calibration_evaluation.json").is_file()
            )
            self.assertTrue(Path(directory, "holdout_predictions.csv").is_file())

    def test_parameter_split_improves_production_development_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_calibration_experiment(
                self.ROOT / "dataset/postgresql/production_control.csv",
                self.ROOT / "models/xgboost/xgboost_query_time.joblib",
                directory,
                calibration_fraction=0.20,
                seed=1701,
                split_mode="parameter",
            )

        self.assertEqual(result.sql_overlap_count, 0)
        self.assertEqual(result.calibration_template_count, 30)
        self.assertEqual(result.holdout_template_count, 30)
        self.assertLess(result.calibrated.mae_ms, result.baseline.mae_ms)
        self.assertLess(result.calibrated.rmse_ms, result.baseline.rmse_ms)

    def test_conditional_calibration_improves_unseen_template_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_calibration_experiment(
                self.ROOT / "dataset/postgresql/production_control.csv",
                self.ROOT / "models/xgboost/xgboost_query_time.joblib",
                directory,
                calibration_fraction=0.20,
                seed=1701,
                split_mode="unseen-template",
            )

        self.assertLess(result.calibrated.mae_ms, result.baseline.mae_ms)
        self.assertLess(result.calibrated.rmse_ms, result.baseline.rmse_ms)


if __name__ == "__main__":
    unittest.main()
