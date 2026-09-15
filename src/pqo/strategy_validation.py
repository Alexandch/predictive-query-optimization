"""Repeated unseen-template validation for the strategy selector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import mean, pstdev
import tempfile

from .strategy import (
    StrategyTrainingMetrics,
    summarize_strategy_experience,
    train_strategy_classifier,
)


@dataclass(frozen=True, slots=True)
class MetricDistribution:
    mean: float
    standard_deviation: float
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class StrategyStabilityReport:
    sample_count: int
    template_count: int
    class_counts: dict[str, int]
    seeds: list[int]
    runs: list[StrategyTrainingMetrics]
    accuracy: MetricDistribution
    balanced_accuracy: MetricDistribution
    macro_f1: MetricDistribution
    accuracy_lift_over_majority: MetricDistribution
    readiness_criteria: dict[str, float | int | bool]
    ready_for_ui_trial: bool


def evaluate_strategy_stability(
    experience_path: str | Path,
    output_path: str | Path,
    *,
    seeds: tuple[int, ...] = (7, 21, 42, 84, 168),
) -> StrategyStabilityReport:
    """Evaluate multiple grouped splits and write one auditable report."""
    if len(set(seeds)) < 3:
        raise ValueError("Stability validation requires at least three unique seeds")
    with tempfile.TemporaryDirectory(prefix="pqo-strategy-validation-") as directory:
        runs = [
            train_strategy_classifier(
                experience_path,
                Path(directory) / f"seed-{seed}",
                seed=seed,
            )
            for seed in seeds
        ]
    summary = summarize_strategy_experience(experience_path)
    report = aggregate_strategy_metrics(summary, runs, list(seeds))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def aggregate_strategy_metrics(
    dataset_summary: dict,
    runs: list[StrategyTrainingMetrics],
    seeds: list[int],
) -> StrategyStabilityReport:
    if not runs or len(runs) != len(seeds):
        raise ValueError("Every validation seed must have one metrics record")

    accuracy = _distribution([run.accuracy for run in runs])
    balanced = _distribution([run.balanced_accuracy for run in runs])
    macro_f1 = _distribution([run.macro_f1 for run in runs])
    lift = _distribution(
        [run.accuracy - run.majority_accuracy for run in runs]
    )
    class_counts = {
        str(label): int(count)
        for label, count in dataset_summary["class_counts"].items()
    }
    criteria: dict[str, float | int | bool] = {
        "minimum_samples": 50,
        "minimum_samples_per_class": 8,
        "minimum_mean_balanced_accuracy": 0.60,
        "minimum_worst_balanced_accuracy": 0.45,
        "minimum_mean_macro_f1": 0.55,
        "minimum_mean_accuracy_lift": 0.05,
        "sample_count_passed": int(dataset_summary["sample_count"]) >= 50,
        "class_balance_passed": min(class_counts.values()) >= 8,
        "mean_balanced_accuracy_passed": balanced.mean >= 0.60,
        "worst_balanced_accuracy_passed": balanced.minimum >= 0.45,
        "mean_macro_f1_passed": macro_f1.mean >= 0.55,
        "mean_accuracy_lift_passed": lift.mean >= 0.05,
    }
    readiness_keys = [key for key in criteria if key.endswith("_passed")]
    ready = all(bool(criteria[key]) for key in readiness_keys)
    return StrategyStabilityReport(
        sample_count=int(dataset_summary["sample_count"]),
        template_count=int(dataset_summary["template_count"]),
        class_counts=class_counts,
        seeds=seeds,
        runs=runs,
        accuracy=accuracy,
        balanced_accuracy=balanced,
        macro_f1=macro_f1,
        accuracy_lift_over_majority=lift,
        readiness_criteria=criteria,
        ready_for_ui_trial=ready,
    )


def _distribution(values: list[float]) -> MetricDistribution:
    return MetricDistribution(
        mean=float(mean(values)),
        standard_deviation=float(pstdev(values)),
        minimum=float(min(values)),
        maximum=float(max(values)),
    )
