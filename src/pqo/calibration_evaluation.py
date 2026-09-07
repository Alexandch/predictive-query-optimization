"""Leakage-safe retrospective evaluation of database calibration."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import random
from statistics import fmean, pstdev

from .calibration import (
    CalibrationProfile,
    add_observation,
    apply_calibration,
    new_profile,
    save_profile,
)
from .config import DatabaseSettings
from .training import GROUP_COLUMN, MODEL_FEATURES, TARGET_COLUMN


@dataclass(frozen=True, slots=True)
class PredictionMetrics:
    mae_ms: float
    rmse_ms: float
    r2: float
    within_20_percent: float


@dataclass(frozen=True, slots=True)
class CalibrationEvaluation:
    dataset_path: str
    seed: int
    split_mode: str
    calibration_template_count: int
    holdout_template_count: int
    calibration_query_count: int
    holdout_query_count: int
    calibration_templates: list[str]
    holdout_templates: list[str]
    sql_overlap_count: int
    factor: float
    active_segment_count: int
    calibrated_holdout_query_count: int
    baseline: PredictionMetrics
    calibrated: PredictionMetrics
    mae_improvement_percent: float
    rmse_improvement_percent: float


@dataclass(frozen=True, slots=True)
class ImprovementDistribution:
    mean: float
    standard_deviation: float
    minimum: float
    maximum: float
    improved_rate_percent: float


@dataclass(frozen=True, slots=True)
class CalibrationRunSummary:
    seed: int
    active_segment_count: int
    calibrated_holdout_query_count: int
    mae_improvement_percent: float
    rmse_improvement_percent: float
    r2_delta: float
    within_20_percent_delta: float


@dataclass(frozen=True, slots=True)
class SplitRobustness:
    split_mode: str
    run_count: int
    passes_stability_gate: bool
    mae_improvement_percent: ImprovementDistribution
    rmse_improvement_percent: ImprovementDistribution
    r2_delta: ImprovementDistribution
    within_20_percent_delta: ImprovementDistribution
    runs: list[CalibrationRunSummary]


@dataclass(frozen=True, slots=True)
class CalibrationCrossValidation:
    dataset_path: str
    calibration_fraction: float
    seeds: list[int]
    sql_overlap_count: int
    stability_gate_minimum_win_rate_percent: float
    splits: dict[str, SplitRobustness]


def _load_unique_predictions(dataset_path: str | Path, model_path: str | Path):
    import joblib
    import numpy as np
    import pandas as pd

    frame = pd.read_csv(dataset_path)
    required = set(MODEL_FEATURES + [GROUP_COLUMN, "sql_text", TARGET_COLUMN])
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Calibration dataset is missing columns: {', '.join(missing)}")
    aggregation = {feature: "first" for feature in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    unique = frame.groupby(
        [GROUP_COLUMN, "sql_text"], as_index=False, sort=False
    ).agg(aggregation)
    artifact = joblib.load(model_path)
    predictions = artifact["pipeline"].predict(
        unique[artifact["model_features"]]
    )
    if artifact.get("target_transform") == "log1p":
        predictions = np.expm1(predictions)
    unique["baseline_prediction_ms"] = np.maximum(
        0.0, predictions.astype(float)
    )
    return unique


def _metrics(actual, predicted) -> PredictionMetrics:
    import numpy as np

    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    errors = np.abs(actual - predicted)
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    r2 = (
        1.0 - float(np.sum((actual - predicted) ** 2)) / denominator
        if denominator
        else 0.0
    )
    return PredictionMetrics(
        mae_ms=float(np.mean(errors)),
        rmse_ms=float(np.sqrt(np.mean((actual - predicted) ** 2))),
        r2=r2,
        within_20_percent=float(
            np.mean(errors / np.maximum(actual, 1.0) <= 0.20) * 100
        ),
    )


def _sql_hash(sql_text: str) -> str:
    return hashlib.sha256(sql_text.strip().encode()).hexdigest()


def run_calibration_experiment(
    dataset_path: str | Path,
    model_path: str | Path,
    output_directory: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    calibration_fraction: float = 0.20,
    seed: int = 1701,
    split_mode: str = "parameter",
    _unique=None,
    _write_files: bool = True,
) -> CalibrationEvaluation:
    """Fit calibration and evaluate it on a disjoint SQL holdout."""
    if not 0 < calibration_fraction < 1:
        raise ValueError("calibration_fraction must be between zero and one")
    settings = settings or DatabaseSettings.from_env()
    unique = (
        _load_unique_predictions(dataset_path, model_path)
        if _unique is None
        else _unique
    )
    templates = sorted(unique[GROUP_COLUMN].astype(str).unique().tolist())
    if len(templates) < 4:
        raise ValueError("Calibration experiment requires at least four templates")
    randomizer = random.Random(seed)
    if split_mode == "unseen-template":
        randomizer.shuffle(templates)
        calibration_count = max(1, round(len(templates) * calibration_fraction))
        calibration_templates = sorted(templates[:calibration_count])
        holdout_templates = sorted(templates[calibration_count:])
        calibration_rows = unique[
            unique[GROUP_COLUMN].astype(str).isin(calibration_templates)
        ]
        holdout_rows = unique[
            unique[GROUP_COLUMN].astype(str).isin(holdout_templates)
        ]
    elif split_mode == "parameter":
        calibration_indices = []
        for _, group in unique.groupby(GROUP_COLUMN, sort=True):
            indices = group.index.tolist()
            randomizer.shuffle(indices)
            calibration_indices.extend(
                indices[: max(1, round(len(indices) * calibration_fraction))]
            )
        calibration_rows = unique.loc[sorted(calibration_indices)]
        holdout_rows = unique.drop(calibration_indices)
        calibration_templates = sorted(
            calibration_rows[GROUP_COLUMN].astype(str).unique().tolist()
        )
        holdout_templates = sorted(
            holdout_rows[GROUP_COLUMN].astype(str).unique().tolist()
        )
    else:
        raise ValueError("split_mode must be 'parameter' or 'unseen-template'")
    if len(calibration_rows) < 3:
        raise ValueError("Calibration split must contain at least three unique SQL queries")
    if holdout_rows.empty:
        raise ValueError("Holdout split must not be empty")

    profile = new_profile(settings, model_path)
    for _, row in calibration_rows.iterrows():
        profile, _ = add_observation(
            profile,
            str(row["sql_text"]),
            float(row["baseline_prediction_ms"]),
            float(row[TARGET_COLUMN]),
        )
    if not profile.ready:
        raise ValueError("Calibration split did not activate the profile")

    calibration_hashes = {
        _sql_hash(str(value)) for value in calibration_rows["sql_text"]
    }
    holdout_hashes = {_sql_hash(str(value)) for value in holdout_rows["sql_text"]}
    overlap = calibration_hashes & holdout_hashes
    if overlap:
        raise ValueError("Calibration and holdout SQL sets overlap")

    actual = holdout_rows[TARGET_COLUMN].astype(float).to_numpy()
    baseline_predictions = holdout_rows["baseline_prediction_ms"].to_numpy()
    calibrated_predictions = [
        apply_calibration(value, profile, str(row["sql_text"]))
        for value, (_, row) in zip(
            baseline_predictions, holdout_rows.iterrows(), strict=True
        )
    ]
    calibrated_holdout_query_count = int(
        sum(
            calibrated_value != baseline_value
            for calibrated_value, baseline_value in zip(
                calibrated_predictions, baseline_predictions, strict=True
            )
        )
    )
    baseline = _metrics(actual, baseline_predictions)
    calibrated = _metrics(actual, calibrated_predictions)

    def improvement(before: float, after: float) -> float:
        return 100.0 * (before - after) / before if before else 0.0

    result = CalibrationEvaluation(
        dataset_path=str(Path(dataset_path)),
        seed=seed,
        split_mode=split_mode,
        calibration_template_count=len(calibration_templates),
        holdout_template_count=len(holdout_templates),
        calibration_query_count=len(calibration_rows),
        holdout_query_count=len(holdout_rows),
        calibration_templates=calibration_templates,
        holdout_templates=holdout_templates,
        sql_overlap_count=len(overlap),
        factor=profile.factor,
        active_segment_count=profile.active_segment_count,
        calibrated_holdout_query_count=calibrated_holdout_query_count,
        baseline=baseline,
        calibrated=calibrated,
        mae_improvement_percent=improvement(
            baseline.mae_ms, calibrated.mae_ms
        ),
        rmse_improvement_percent=improvement(
            baseline.rmse_ms, calibrated.rmse_ms
        ),
    )
    if _write_files:
        _write_outputs(
            output_directory,
            profile,
            result,
            holdout_rows,
            actual,
            baseline_predictions,
            calibrated_predictions,
        )
    return result


def _distribution(values: list[float]) -> ImprovementDistribution:
    return ImprovementDistribution(
        mean=fmean(values),
        standard_deviation=pstdev(values),
        minimum=min(values),
        maximum=max(values),
        improved_rate_percent=100.0 * sum(value > 0 for value in values) / len(values),
    )


def run_calibration_cross_validation(
    dataset_path: str | Path,
    model_path: str | Path,
    output_directory: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    calibration_fraction: float = 0.20,
    seeds: list[int] | None = None,
    split_modes: tuple[str, ...] = ("parameter", "unseen-template"),
    minimum_win_rate_percent: float = 70.0,
) -> CalibrationCrossValidation:
    """Evaluate calibration stability over repeated leakage-safe splits."""
    seeds = list(range(1, 21)) if seeds is None else list(seeds)
    if not seeds:
        raise ValueError("At least one cross-validation seed is required")
    if len(set(seeds)) != len(seeds):
        raise ValueError("Cross-validation seeds must be unique")
    if not 0 <= minimum_win_rate_percent <= 100:
        raise ValueError("minimum_win_rate_percent must be between 0 and 100")
    invalid_modes = set(split_modes).difference({"parameter", "unseen-template"})
    if invalid_modes or not split_modes:
        raise ValueError("split_modes must contain parameter and/or unseen-template")

    settings = settings or DatabaseSettings.from_env()
    unique = _load_unique_predictions(dataset_path, model_path)
    split_results: dict[str, SplitRobustness] = {}
    total_overlap = 0
    for split_mode in split_modes:
        evaluations = [
            run_calibration_experiment(
                dataset_path,
                model_path,
                output_directory,
                settings,
                calibration_fraction=calibration_fraction,
                seed=seed,
                split_mode=split_mode,
                _unique=unique,
                _write_files=False,
            )
            for seed in seeds
        ]
        total_overlap += sum(item.sql_overlap_count for item in evaluations)
        runs = [
            CalibrationRunSummary(
                seed=item.seed,
                active_segment_count=item.active_segment_count,
                calibrated_holdout_query_count=item.calibrated_holdout_query_count,
                mae_improvement_percent=item.mae_improvement_percent,
                rmse_improvement_percent=item.rmse_improvement_percent,
                r2_delta=item.calibrated.r2 - item.baseline.r2,
                within_20_percent_delta=(
                    item.calibrated.within_20_percent
                    - item.baseline.within_20_percent
                ),
            )
            for item in evaluations
        ]
        mae = _distribution([item.mae_improvement_percent for item in runs])
        rmse = _distribution([item.rmse_improvement_percent for item in runs])
        split_results[split_mode] = SplitRobustness(
            split_mode=split_mode,
            run_count=len(runs),
            passes_stability_gate=(
                mae.mean > 0
                and rmse.mean > 0
                and mae.improved_rate_percent >= minimum_win_rate_percent
                and rmse.improved_rate_percent >= minimum_win_rate_percent
            ),
            mae_improvement_percent=mae,
            rmse_improvement_percent=rmse,
            r2_delta=_distribution([item.r2_delta for item in runs]),
            within_20_percent_delta=_distribution(
                [item.within_20_percent_delta for item in runs]
            ),
            runs=runs,
        )

    result = CalibrationCrossValidation(
        dataset_path=str(Path(dataset_path)),
        calibration_fraction=calibration_fraction,
        seeds=seeds,
        sql_overlap_count=total_overlap,
        stability_gate_minimum_win_rate_percent=minimum_win_rate_percent,
        splits=split_results,
    )
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "calibration_cross_validation.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def _write_outputs(
    output_directory: str | Path,
    profile: CalibrationProfile,
    result: CalibrationEvaluation,
    holdout_rows,
    actual,
    baseline_predictions,
    calibrated_predictions,
) -> None:
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    save_profile(profile, destination / "calibration_profile.json")
    (destination / "calibration_evaluation.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (destination / "holdout_predictions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "template_id",
                "sql_text",
                "actual_ms",
                "baseline_ms",
                "calibrated_ms",
                "baseline_ae_ms",
                "calibrated_ae_ms",
            ]
        )
        for position, (_, row) in enumerate(holdout_rows.iterrows()):
            writer.writerow(
                [
                    row[GROUP_COLUMN],
                    row["sql_text"],
                    actual[position],
                    baseline_predictions[position],
                    calibrated_predictions[position],
                    abs(actual[position] - baseline_predictions[position]),
                    abs(actual[position] - calibrated_predictions[position]),
                ]
            )
