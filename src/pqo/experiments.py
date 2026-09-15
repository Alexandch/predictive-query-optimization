"""Experiment reports, exports and guarded model promotion for the desktop UI."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable

from .history import HistoryRecord
from .training import GROUP_COLUMN, MODEL_FEATURES, TARGET_COLUMN


@dataclass(frozen=True, slots=True)
class PredictionPoint:
    template_id: str
    sql_text: str
    actual_ms: float
    predicted_ms: float

    @property
    def absolute_error_ms(self) -> float:
        return abs(self.actual_ms - self.predicted_ms)


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    created_at: str
    xgboost_metrics: dict[str, Any]
    dqn_metrics: dict[str, Any]
    dqn_stress_metrics: dict[str, Any]
    prediction_points: tuple[PredictionPoint, ...]
    dataset_mae_ms: float
    dataset_rmse_ms: float
    dataset_r2: float

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["prediction_points"] = [
            {**asdict(point), "absolute_error_ms": point.absolute_error_ms}
            for point in self.prediction_points
        ]
        return result


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    allowed: bool
    explanation: str


def _load_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Report not found: {source}")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Report must contain a JSON object: {source}")
    return value


def build_experiment_report(
    xgboost_metrics_path: str | Path,
    dqn_metrics_path: str | Path,
    dqn_stress_metrics_path: str | Path,
    model_path: str | Path,
    dataset_path: str | Path,
) -> ExperimentReport:
    """Load persisted reports and evaluate XGBoost on unique SQL statements."""
    import joblib
    import numpy as np
    import pandas as pd

    xgboost_metrics = _load_json_object(xgboost_metrics_path)
    dqn_metrics = _load_json_object(dqn_metrics_path)
    dqn_stress_metrics = _load_json_object(dqn_stress_metrics_path)
    try:
        for section in ("parameter_holdout", "unseen_template_stress"):
            for metric in ("r2", "mae_ms", "within_20_percent"):
                xgboost_metrics[section][metric]
        for report in (dqn_metrics, dqn_stress_metrics):
            for metric in (
                "recommendation_accuracy",
                "mean_regret",
                "random_accuracy",
                "noop_accuracy",
            ):
                report[metric]
    except KeyError as exc:
        raise ValueError(f"Experiment report is missing metric: {exc}") from exc
    artifact = joblib.load(model_path)
    frame = pd.read_csv(dataset_path)

    required = set(MODEL_FEATURES + [GROUP_COLUMN, "sql_text", TARGET_COLUMN])
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Dataset is missing columns: {', '.join(missing)}")

    aggregation = {feature: "first" for feature in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    unique = frame.groupby(
        [GROUP_COLUMN, "sql_text"], as_index=False, sort=False
    ).agg(aggregation)
    features = unique[artifact["model_features"]]
    actual = unique[TARGET_COLUMN].astype(float).to_numpy()
    predicted = artifact["pipeline"].predict(features)
    if artifact.get("target_transform") == "log1p":
        predicted = np.expm1(predicted)
    predicted = np.maximum(0.0, predicted.astype(float))

    residuals = actual - predicted
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    r2 = 1.0 - float(np.sum(residuals**2)) / denominator if denominator else 0.0
    points = tuple(
        PredictionPoint(
            template_id=str(row[GROUP_COLUMN]),
            sql_text=str(row["sql_text"]),
            actual_ms=float(actual[index]),
            predicted_ms=float(predicted[index]),
        )
        for index, (_, row) in enumerate(unique.iterrows())
    )
    return ExperimentReport(
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        xgboost_metrics=xgboost_metrics,
        dqn_metrics=dqn_metrics,
        dqn_stress_metrics=dqn_stress_metrics,
        prediction_points=points,
        dataset_mae_ms=float(np.mean(np.abs(residuals))),
        dataset_rmse_ms=float(np.sqrt(np.mean(residuals**2))),
        dataset_r2=r2,
    )


def assess_candidate(
    model_kind: str,
    candidate_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
) -> PromotionDecision:
    """Allow replacement only when every primary quality criterion is no worse."""
    if model_kind == "xgboost":
        sections = ("parameter_holdout", "unseen_template_stress")
        for section in sections:
            candidate = candidate_metrics.get(section, {})
            baseline = baseline_metrics.get(section, {})
            if candidate.get("r2", float("-inf")) < baseline.get("r2", float("inf")):
                return PromotionDecision(False, f"R² ухудшился в {section}.")
            if candidate.get("mae_ms", float("inf")) > baseline.get("mae_ms", float("-inf")):
                return PromotionDecision(False, f"MAE ухудшилась в {section}.")
        return PromotionDecision(True, "R² и MAE не хуже текущей XGBoost-модели.")

    if model_kind == "dqn":
        if candidate_metrics.get("split_mode") != "parameter":
            return PromotionDecision(
                False,
                "Stress-модель нельзя назначить основной без parameter-оценки.",
            )
        if candidate_metrics.get("recommendation_accuracy", -1.0) < baseline_metrics.get(
            "recommendation_accuracy", 2.0
        ):
            return PromotionDecision(False, "Recommendation accuracy стала ниже.")
        if candidate_metrics.get("mean_regret", float("inf")) > baseline_metrics.get(
            "mean_regret", float("-inf")
        ):
            return PromotionDecision(False, "Mean regret стал выше.")
        return PromotionDecision(True, "Accuracy и regret не хуже текущей DQN-модели.")

    raise ValueError("model_kind must be 'xgboost' or 'dqn'")


def promote_candidate_model(
    model_kind: str,
    candidate_directory: str | Path,
    primary_directory: str | Path,
) -> PromotionDecision:
    candidate = Path(candidate_directory)
    primary = Path(primary_directory)
    decision = assess_candidate(
        model_kind,
        _load_json_object(candidate / "metrics.json"),
        _load_json_object(primary / "metrics.json"),
    )
    if not decision.allowed:
        return decision

    filenames = (
        ("xgboost_query_time.joblib", "metrics.json", "feature_importance.json")
        if model_kind == "xgboost"
        else ("dqn_index_advisor.pt", "metrics.json")
    )
    missing = [name for name in filenames if not (candidate / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Candidate artifacts are missing: {', '.join(missing)}")
    primary.mkdir(parents=True, exist_ok=True)
    backup = primary / "previous"
    backup.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        current = primary / filename
        if current.is_file():
            shutil.copy2(current, backup / filename)
        temporary = primary / f".{filename}.candidate"
        shutil.copy2(candidate / filename, temporary)
        os.replace(temporary, current)
    return decision


def export_experiment_report(report: ExperimentReport, output_path: str | Path) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".json":
        destination.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return
    if destination.suffix.lower() != ".csv":
        raise ValueError("Experiment export must use .csv or .json")
    with destination.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "template_id",
                "actual_ms",
                "predicted_ms",
                "absolute_error_ms",
                "sql_text",
            ],
        )
        writer.writeheader()
        for point in report.prediction_points:
            writer.writerow(
                {
                    **asdict(point),
                    "absolute_error_ms": point.absolute_error_ms,
                }
            )
    summary_path = destination.with_name(f"{destination.stem}_summary.csv")
    parameter = report.xgboost_metrics.get("parameter_holdout", {})
    stress = report.xgboost_metrics.get("unseen_template_stress", {})
    with summary_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["model", "evaluation", "metric", "value"])
        writer.writerows(
            [
                ["xgboost", "dataset", "mae_ms", report.dataset_mae_ms],
                ["xgboost", "dataset", "rmse_ms", report.dataset_rmse_ms],
                ["xgboost", "dataset", "r2", report.dataset_r2],
                ["xgboost", "parameter", "r2", parameter.get("r2")],
                ["xgboost", "stress", "r2", stress.get("r2")],
                ["dqn", "parameter", "accuracy", report.dqn_metrics.get("recommendation_accuracy")],
                ["dqn", "parameter", "mean_regret", report.dqn_metrics.get("mean_regret")],
                ["dqn", "stress", "accuracy", report.dqn_stress_metrics.get("recommendation_accuracy")],
                ["dqn", "stress", "mean_regret", report.dqn_stress_metrics.get("mean_regret")],
            ]
        )


def export_history_records(
    records: Iterable[HistoryRecord], output_path: str | Path
) -> None:
    rows = [asdict(record) for record in records]
    for row in rows:
        row["started_at"] = row["started_at"].isoformat()
        row["recommended_columns"] = list(row["recommended_columns"])
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".json":
        destination.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    if destination.suffix.lower() != ".csv":
        raise ValueError("History export must use .csv or .json")
    fieldnames = [
        "query_run_id",
        "started_at",
        "status",
        "sql_text",
        "predicted_time_ms",
        "root_node_type",
        "recommended_table",
        "recommended_columns",
        "predicted_reward",
        "sequential_analysis_id",
        "measured_baseline_time_ms",
        "measured_final_time_ms",
        "measured_improvement_ratio",
        "sequential_terminal_reason",
        "sequential_steps",
    ]
    with destination.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row["recommended_columns"] = ", ".join(row["recommended_columns"])
            row["sequential_steps"] = json.dumps(
                row["sequential_steps"], ensure_ascii=False
            )
            writer.writerow(row)
