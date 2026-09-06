"""Leakage-safe retrospective evaluation of database calibration."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import random

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
    baseline: PredictionMetrics
    calibrated: PredictionMetrics
    mae_improvement_percent: float
    rmse_improvement_percent: float


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
) -> CalibrationEvaluation:
    """Fit calibration and evaluate it on a disjoint SQL holdout."""
    if not 0 < calibration_fraction < 1:
        raise ValueError("calibration_fraction must be between zero and one")
    settings = settings or DatabaseSettings.from_env()
    unique = _load_unique_predictions(dataset_path, model_path)
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
        baseline=baseline,
        calibrated=calibrated,
        mae_improvement_percent=improvement(
            baseline.mae_ms, calibrated.mae_ms
        ),
        rmse_improvement_percent=improvement(
            baseline.rmse_ms, calibrated.rmse_ms
        ),
    )
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
