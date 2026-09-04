"""Evaluation of frozen XGBoost and DQN models on an independent workload."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import random
from typing import Any

from .dqn import predict_action_values
from .training import GROUP_COLUMN, MODEL_FEATURES, TARGET_COLUMN


@dataclass(frozen=True, slots=True)
class ControlXGBoostMetrics:
    sample_count: int
    unique_query_count: int
    template_count: int
    mae_ms: float
    median_ae_ms: float
    p90_ae_ms: float
    rmse_ms: float
    r2: float
    mape_percent: float
    within_20_percent: float
    actual_min_ms: float
    actual_median_ms: float
    actual_max_ms: float


@dataclass(frozen=True, slots=True)
class ControlDQNMetrics:
    query_count: int
    action_count: int
    template_count: int
    reward_mae: float
    recommendation_accuracy: float
    mean_regret: float
    noop_accuracy: float
    noop_mean_regret: float
    random_accuracy: float
    random_mean_regret: float


def evaluate_xgboost_control(
    dataset_path: str | Path,
    model_path: str | Path,
    output_directory: str | Path,
) -> ControlXGBoostMetrics:
    import joblib
    import numpy as np
    import pandas as pd

    frame = pd.read_csv(dataset_path)
    required = set(MODEL_FEATURES + [GROUP_COLUMN, "sql_text", TARGET_COLUMN])
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Control dataset is missing columns: {', '.join(missing)}")
    raw_count = len(frame)
    aggregation = {feature: "first" for feature in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    unique = frame.groupby([GROUP_COLUMN, "sql_text"], as_index=False, sort=False).agg(
        aggregation
    )
    artifact = joblib.load(model_path)
    predicted = artifact["pipeline"].predict(unique[artifact["model_features"]])
    if artifact.get("target_transform") == "log1p":
        predicted = np.expm1(predicted)
    predicted = np.maximum(0.0, predicted.astype(float))
    actual = unique[TARGET_COLUMN].astype(float).to_numpy()
    errors = np.abs(actual - predicted)
    relative = errors / np.maximum(actual, 1.0)
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    r2 = 1.0 - float(np.sum((actual - predicted) ** 2)) / denominator if denominator else 0.0
    metrics = ControlXGBoostMetrics(
        sample_count=raw_count,
        unique_query_count=len(unique),
        template_count=int(unique[GROUP_COLUMN].nunique()),
        mae_ms=float(np.mean(errors)),
        median_ae_ms=float(np.median(errors)),
        p90_ae_ms=float(np.percentile(errors, 90)),
        rmse_ms=float(np.sqrt(np.mean((actual - predicted) ** 2))),
        r2=r2,
        mape_percent=float(np.mean(relative) * 100),
        within_20_percent=float(np.mean(relative <= 0.20) * 100),
        actual_min_ms=float(np.min(actual)),
        actual_median_ms=float(np.median(actual)),
        actual_max_ms=float(np.max(actual)),
    )
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "xgboost_control_metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (destination / "xgboost_control_predictions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["template_id", "sql_text", "actual_ms", "predicted_ms", "absolute_error_ms"]
        )
        for index, row in unique.iterrows():
            writer.writerow(
                [
                    row[GROUP_COLUMN],
                    row["sql_text"],
                    actual[index],
                    predicted[index],
                    errors[index],
                ]
            )
    unique = unique.assign(predicted_ms=predicted, absolute_error_ms=errors)
    by_template = (
        unique.groupby(GROUP_COLUMN)
        .agg(
            query_count=("sql_text", "size"),
            mae_ms=("absolute_error_ms", "mean"),
            median_ae_ms=("absolute_error_ms", "median"),
            actual_median_ms=(TARGET_COLUMN, "median"),
            actual_max_ms=(TARGET_COLUMN, "max"),
        )
        .sort_values("mae_ms", ascending=False)
    )
    by_template.to_csv(destination / "xgboost_control_by_template.csv")
    return metrics


def evaluate_dqn_control(
    experience_path: str | Path,
    model_path: str | Path,
    output_directory: str | Path,
    *,
    seed: int = 42,
) -> ControlDQNMetrics:
    import numpy as np

    with Path(experience_path).open(encoding="utf-8") as stream:
        records: list[dict[str, Any]] = [json.loads(line) for line in stream if line.strip()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record["query_id"], []).append(record)
    groups = {query_id: items for query_id, items in groups.items() if len(items) >= 2}
    if not groups:
        raise ValueError("DQN control experience contains no queries with candidate actions")

    correct = noop_correct = random_correct = 0
    regrets: list[float] = []
    noop_regrets: list[float] = []
    random_regrets: list[float] = []
    absolute_errors: list[float] = []
    randomizer = random.Random(seed)
    action_count = 0
    templates: set[str] = set()
    decision_rows: list[dict[str, Any]] = []
    for candidates in groups.values():
        values = predict_action_values(
            model_path,
            candidates[0]["state"],
            [candidate["action_features"] for candidate in candidates],
        )
        rewards = [float(candidate["reward"]) for candidate in candidates]
        predicted_index = int(np.argmax(values))
        noop_index = next(
            index
            for index, candidate in enumerate(candidates)
            if candidate["action"]["kind"] == "noop"
        )
        if predicted_index != noop_index and values[predicted_index] <= 0:
            predicted_index = noop_index
        actual_index = int(np.argmax(rewards))
        random_index = randomizer.randrange(len(candidates))
        correct += predicted_index == actual_index
        noop_correct += noop_index == actual_index
        random_correct += random_index == actual_index
        regrets.append(rewards[actual_index] - rewards[predicted_index])
        noop_regrets.append(rewards[actual_index] - rewards[noop_index])
        random_regrets.append(rewards[actual_index] - rewards[random_index])
        absolute_errors.extend(abs(actual - predicted) for actual, predicted in zip(rewards, values))
        action_count += len(candidates)
        templates.add(candidates[0]["template_id"])
        decision_rows.append(
            {
                "template_id": candidates[0]["template_id"],
                "query_id": candidates[0]["query_id"],
                "predicted_action": json.dumps(candidates[predicted_index]["action"], ensure_ascii=False),
                "actual_best_action": json.dumps(candidates[actual_index]["action"], ensure_ascii=False),
                "predicted_action_reward": rewards[predicted_index],
                "actual_best_reward": rewards[actual_index],
                "regret": rewards[actual_index] - rewards[predicted_index],
                "is_correct": predicted_index == actual_index,
            }
        )
    query_count = len(groups)
    metrics = ControlDQNMetrics(
        query_count=query_count,
        action_count=action_count,
        template_count=len(templates),
        reward_mae=float(np.mean(absolute_errors)),
        recommendation_accuracy=correct / query_count,
        mean_regret=float(np.mean(regrets)),
        noop_accuracy=noop_correct / query_count,
        noop_mean_regret=float(np.mean(noop_regrets)),
        random_accuracy=random_correct / query_count,
        random_mean_regret=float(np.mean(random_regrets)),
    )
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "dqn_control_metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (destination / "dqn_control_decisions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(decision_rows[0]))
        writer.writeheader()
        writer.writerows(decision_rows)
    return metrics
