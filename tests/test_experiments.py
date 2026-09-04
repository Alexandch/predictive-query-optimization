from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from pqo.experiments import (
    ExperimentReport,
    PredictionPoint,
    assess_candidate,
    export_experiment_report,
    export_history_records,
    promote_candidate_model,
)
from pqo.history import HistoryRecord


class ExperimentTests(unittest.TestCase):
    def test_xgboost_candidate_must_improve_every_primary_metric(self):
        baseline = {
            "parameter_holdout": {"r2": 0.90, "mae_ms": 12.0},
            "unseen_template_stress": {"r2": 0.80, "mae_ms": 20.0},
        }
        better = {
            "parameter_holdout": {"r2": 0.91, "mae_ms": 11.0},
            "unseen_template_stress": {"r2": 0.82, "mae_ms": 19.0},
        }
        mixed = {
            "parameter_holdout": {"r2": 0.92, "mae_ms": 10.0},
            "unseen_template_stress": {"r2": 0.79, "mae_ms": 18.0},
        }
        self.assertTrue(assess_candidate("xgboost", better, baseline).allowed)
        self.assertFalse(assess_candidate("xgboost", mixed, baseline).allowed)

    def test_stress_dqn_cannot_replace_primary_model(self):
        candidate = {
            "split_mode": "unseen-template",
            "recommendation_accuracy": 1.0,
            "mean_regret": 0.0,
        }
        baseline = {"recommendation_accuracy": 0.5, "mean_regret": 0.1}
        self.assertFalse(assess_candidate("dqn", candidate, baseline).allowed)

    def test_promotion_keeps_previous_artifacts(self):
        baseline = {
            "parameter_holdout": {"r2": 0.80, "mae_ms": 20.0},
            "unseen_template_stress": {"r2": 0.70, "mae_ms": 30.0},
        }
        candidate = {
            "parameter_holdout": {"r2": 0.90, "mae_ms": 10.0},
            "unseen_template_stress": {"r2": 0.80, "mae_ms": 20.0},
        }
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            primary = root / "primary"
            new = root / "candidate"
            primary.mkdir()
            new.mkdir()
            (primary / "metrics.json").write_text(json.dumps(baseline), encoding="utf-8")
            (primary / "xgboost_query_time.joblib").write_text("old", encoding="utf-8")
            (primary / "feature_importance.json").write_text("old", encoding="utf-8")
            (new / "metrics.json").write_text(json.dumps(candidate), encoding="utf-8")
            (new / "xgboost_query_time.joblib").write_text("new", encoding="utf-8")
            (new / "feature_importance.json").write_text("new", encoding="utf-8")

            decision = promote_candidate_model("xgboost", new, primary)

            self.assertTrue(decision.allowed)
            self.assertEqual((primary / "xgboost_query_time.joblib").read_text(), "new")
            self.assertEqual(
                (primary / "previous" / "xgboost_query_time.joblib").read_text(),
                "old",
            )

    def test_exports_experiment_and_history(self):
        report = ExperimentReport(
            created_at="2026-01-01T00:00:00+03:00",
            xgboost_metrics={},
            dqn_metrics={},
            dqn_stress_metrics={},
            prediction_points=(PredictionPoint("t1", "SELECT 1", 2.0, 2.5),),
            dataset_mae_ms=0.5,
            dataset_rmse_ms=0.5,
            dataset_r2=0.9,
        )
        history = [
            HistoryRecord(
                1,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                "completed",
                "SELECT 1",
                2.5,
                "Result",
                None,
                (),
                None,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            export_experiment_report(report, root / "report.json")
            export_experiment_report(report, root / "points.csv")
            export_history_records(history, root / "history.json")
            export_history_records(history, root / "history.csv")
            self.assertEqual(
                json.loads((root / "report.json").read_text(encoding="utf-8"))[
                    "prediction_points"
                ][0]["absolute_error_ms"],
                0.5,
            )
            self.assertIn("SELECT 1", (root / "points.csv").read_text(encoding="utf-8-sig"))
            self.assertTrue((root / "points_summary.csv").is_file())
            self.assertIn("completed", (root / "history.csv").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
