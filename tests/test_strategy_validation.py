import unittest

from pqo.strategy import StrategyTrainingMetrics
from pqo.strategy_validation import aggregate_strategy_metrics


def metrics(seed, balanced=0.72, macro_f1=0.70, accuracy=0.74, majority=0.38):
    return StrategyTrainingMetrics(
        sample_count=84,
        train_count=56,
        test_count=28,
        train_templates=["train"],
        test_templates=["test"],
        class_counts={"NOOP": 28, "CREATE_INDEX": 33, "REWRITE_QUERY": 23},
        accuracy=accuracy,
        balanced_accuracy=balanced,
        macro_f1=macro_f1,
        majority_accuracy=majority,
        confusion_matrix=[[8, 1, 0], [1, 9, 1], [0, 1, 7]],
        labels=["NOOP", "CREATE_INDEX", "REWRITE_QUERY"],
        seed=seed,
    )


class StrategyValidationTests(unittest.TestCase):
    def test_marks_stable_dataset_ready_for_ui_trial(self):
        summary = {
            "sample_count": 84,
            "template_count": 58,
            "class_counts": {"NOOP": 28, "CREATE_INDEX": 33, "REWRITE_QUERY": 23},
        }

        report = aggregate_strategy_metrics(
            summary,
            [metrics(7), metrics(21), metrics(42)],
            [7, 21, 42],
        )

        self.assertTrue(report.ready_for_ui_trial)
        self.assertAlmostEqual(report.accuracy_lift_over_majority.mean, 0.36)

    def test_rejects_unstable_worst_split(self):
        summary = {
            "sample_count": 84,
            "template_count": 58,
            "class_counts": {"NOOP": 28, "CREATE_INDEX": 33, "REWRITE_QUERY": 23},
        }

        report = aggregate_strategy_metrics(
            summary,
            [metrics(7), metrics(21, balanced=0.30), metrics(42)],
            [7, 21, 42],
        )

        self.assertFalse(report.ready_for_ui_trial)
        self.assertFalse(
            report.readiness_criteria["worst_balanced_accuracy_passed"]
        )


if __name__ == "__main__":
    unittest.main()
