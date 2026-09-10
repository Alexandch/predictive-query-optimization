"""Development-only selection of an additional-domain training fraction."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import random
from typing import Iterable

from .training import (
    CATEGORICAL_FEATURES,
    GROUP_COLUMN,
    MODEL_FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    _validate_dataset,
)


DEVELOPMENT_DOMAINS = ("aviation", "retail", "pagila")


@dataclass(frozen=True, slots=True)
class DomainMetric:
    domain: str
    sample_count: int
    mae_ms: float
    normalized_mae: float
    r2: float


@dataclass(frozen=True, slots=True)
class MixRun:
    fraction: float
    seed: int
    train_count: int
    pagila_train_count: int
    macro_normalized_mae: float
    macro_r2: float
    domains: list[DomainMetric]


@dataclass(frozen=True, slots=True)
class FractionSummary:
    fraction: float
    mean_macro_normalized_mae: float
    mean_macro_r2: float
    worst_macro_normalized_mae: float


@dataclass(frozen=True, slots=True)
class DomainMixReport:
    selection_protocol: str
    fractions: list[float]
    seeds: list[int]
    runs: list[MixRun]
    summaries: list[FractionSummary]
    selected_fraction: float


def infer_development_domain(template_id: str) -> str:
    if template_id.startswith("pagila_"):
        return "pagila"
    if template_id.startswith("retail_"):
        return "retail"
    return "aviation"


def evaluate_xgboost_domain_mix(
    base_dataset: str | Path,
    pagila_dataset: str | Path,
    output_directory: str | Path,
    *,
    fractions: Iterable[float] = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
    seeds: Iterable[int] = (21, 42, 84),
) -> DomainMixReport:
    """Select a Pagila fraction without reading any external control set."""
    import numpy as np
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder
    from xgboost import XGBRegressor

    fractions = sorted(set(float(value) for value in fractions))
    seeds = list(dict.fromkeys(int(value) for value in seeds))
    if not fractions or any(value < 0.0 or value > 1.0 for value in fractions):
        raise ValueError("fractions must contain values in the interval [0, 1]")
    if not seeds:
        raise ValueError("at least one seed is required")

    raw_base = pd.read_csv(base_dataset)
    raw_pagila = pd.read_csv(pagila_dataset)
    combined = pd.concat([raw_base, raw_pagila], ignore_index=True)
    _validate_dataset(combined)
    aggregation = {feature: "first" for feature in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    frame = (
        combined.groupby([GROUP_COLUMN, "sql_text"], as_index=False, sort=False)
        .agg(aggregation)
    )
    frame["domain"] = frame[GROUP_COLUMN].map(infer_development_domain)
    observed = set(frame["domain"])
    if observed != set(DEVELOPMENT_DOMAINS):
        raise ValueError(f"expected development domains {DEVELOPMENT_DOMAINS}, got {sorted(observed)}")

    def make_pipeline(seed: int):
        return Pipeline(
            steps=[
                (
                    "preprocessor",
                    ColumnTransformer(
                        transformers=[
                            (
                                "root_node",
                                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                                CATEGORICAL_FEATURES,
                            ),
                            ("numeric", "passthrough", NUMERIC_FEATURES),
                        ]
                    ),
                ),
                (
                    "regressor",
                    XGBRegressor(
                        objective="reg:squarederror",
                        n_estimators=500,
                        max_depth=6,
                        learning_rate=0.04,
                        min_child_weight=3,
                        subsample=0.85,
                        colsample_bytree=0.85,
                        reg_lambda=1.0,
                        random_state=seed,
                        n_jobs=4,
                    ),
                ),
            ]
        )

    runs: list[MixRun] = []
    for seed in seeds:
        train_indices, validation_indices = _unseen_template_split(frame, seed)
        for fraction in fractions:
            selected_train = _select_pagila_fraction(
                frame,
                train_indices,
                fraction=fraction,
                seed=seed,
            )
            train = frame.loc[selected_train]
            validation = frame.loc[validation_indices]
            pipeline = make_pipeline(seed)
            pipeline.fit(train[MODEL_FEATURES], train[TARGET_COLUMN].astype(float))
            domain_metrics: list[DomainMetric] = []
            for domain in DEVELOPMENT_DOMAINS:
                domain_frame = validation[validation["domain"] == domain]
                actual = domain_frame[TARGET_COLUMN].astype(float).to_numpy()
                predicted = np.maximum(
                    0.0,
                    pipeline.predict(domain_frame[MODEL_FEATURES]),
                )
                mae = float(mean_absolute_error(actual, predicted))
                domain_metrics.append(
                    DomainMetric(
                        domain=domain,
                        sample_count=len(domain_frame),
                        mae_ms=mae,
                        normalized_mae=mae / max(float(np.mean(actual)), 0.001),
                        r2=float(r2_score(actual, predicted)),
                    )
                )
            runs.append(
                MixRun(
                    fraction=fraction,
                    seed=seed,
                    train_count=len(train),
                    pagila_train_count=int((train["domain"] == "pagila").sum()),
                    macro_normalized_mae=float(
                        np.mean([metric.normalized_mae for metric in domain_metrics])
                    ),
                    macro_r2=float(np.mean([metric.r2 for metric in domain_metrics])),
                    domains=domain_metrics,
                )
            )

    summaries = []
    for fraction in fractions:
        matching = [run for run in runs if run.fraction == fraction]
        summaries.append(
            FractionSummary(
                fraction=fraction,
                mean_macro_normalized_mae=float(
                    np.mean([run.macro_normalized_mae for run in matching])
                ),
                mean_macro_r2=float(np.mean([run.macro_r2 for run in matching])),
                worst_macro_normalized_mae=max(
                    run.macro_normalized_mae for run in matching
                ),
            )
        )
    selected = min(
        summaries,
        key=lambda item: (
            item.mean_macro_normalized_mae,
            item.worst_macro_normalized_mae,
            item.fraction,
        ),
    )
    report = DomainMixReport(
        selection_protocol="development-only unseen-template macro normalized MAE",
        fractions=fractions,
        seeds=seeds,
        runs=runs,
        summaries=summaries,
        selected_fraction=selected.fraction,
    )
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "domain_mix_report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_summary_csv(summaries, destination / "domain_mix_summary.csv")
    build_mixed_dataset(
        raw_base,
        raw_pagila,
        destination / "selected_training.csv",
        fraction=selected.fraction,
        seed=seeds[0],
    )
    return report


def _unseen_template_split(frame, seed: int) -> tuple[list[int], list[int]]:
    train_indices: list[int] = []
    validation_indices: list[int] = []
    for domain in DEVELOPMENT_DOMAINS:
        domain_frame = frame[frame["domain"] == domain]
        templates = sorted(domain_frame[GROUP_COLUMN].unique())
        randomizer = random.Random(f"template-split:{seed}:{domain}")
        randomizer.shuffle(templates)
        validation_count = max(1, math.ceil(len(templates) * 0.25))
        validation_templates = set(templates[:validation_count])
        mask = domain_frame[GROUP_COLUMN].isin(validation_templates)
        validation_indices.extend(domain_frame.index[mask].tolist())
        train_indices.extend(domain_frame.index[~mask].tolist())
    return train_indices, validation_indices


def _select_pagila_fraction(
    frame,
    train_indices: list[int],
    *,
    fraction: float,
    seed: int,
) -> list[int]:
    train = frame.loc[train_indices]
    selected = train[train["domain"] != "pagila"].index.tolist()
    if fraction == 0.0:
        return selected
    pagila = train[train["domain"] == "pagila"]
    for template, group in pagila.groupby(GROUP_COLUMN, sort=True):
        indices = group.index.tolist()
        # Keep fractions nested so their comparison changes only the amount of
        # Pagila data, not the randomly selected query composition.
        randomizer = random.Random(f"pagila-fraction:{seed}:{template}")
        randomizer.shuffle(indices)
        count = max(1, math.ceil(len(indices) * fraction))
        selected.extend(indices[:count])
    return sorted(selected)


def build_mixed_dataset(
    raw_base,
    raw_pagila,
    output_path: str | Path,
    *,
    fraction: float,
    seed: int,
) -> int:
    import pandas as pd

    if fraction == 0.0:
        selected_pagila = raw_pagila.iloc[0:0]
    else:
        selected_sql: set[str] = set()
        unique = raw_pagila[[GROUP_COLUMN, "sql_text"]].drop_duplicates()
        for template, group in unique.groupby(GROUP_COLUMN, sort=True):
            sql_values = group["sql_text"].tolist()
            randomizer = random.Random(f"final-pagila:{seed}:{template}")
            randomizer.shuffle(sql_values)
            count = max(1, math.ceil(len(sql_values) * fraction))
            selected_sql.update(sql_values[:count])
        selected_pagila = raw_pagila[raw_pagila["sql_text"].isin(selected_sql)]
    mixed = pd.concat([raw_base, selected_pagila], ignore_index=True)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    mixed.to_csv(destination, index=False)
    return len(mixed)


def _write_summary_csv(summaries: list[FractionSummary], path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(summaries[0])))
        writer.writeheader()
        writer.writerows(asdict(summary) for summary in summaries)
