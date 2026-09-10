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


@dataclass(frozen=True, slots=True)
class DQNDomainMetric:
    domain: str
    query_count: int
    recommendation_accuracy: float
    mean_regret: float


@dataclass(frozen=True, slots=True)
class DQNMixRun:
    fraction: float
    seed: int
    training_action_count: int
    pagila_training_query_count: int
    macro_accuracy: float
    macro_mean_regret: float
    domains: list[DQNDomainMetric]


@dataclass(frozen=True, slots=True)
class DQNFractionSummary:
    fraction: float
    mean_macro_accuracy: float
    mean_macro_regret: float
    worst_macro_regret: float


@dataclass(frozen=True, slots=True)
class DQNDomainMixReport:
    selection_protocol: str
    fractions: list[float]
    seeds: list[int]
    runs: list[DQNMixRun]
    summaries: list[DQNFractionSummary]
    selected_fraction: float


def infer_development_domain(template_id: str) -> str:
    if template_id.startswith("pagila_"):
        return "pagila"
    if "retail" in template_id:
        return "retail"
    return "aviation"


def evaluate_dqn_domain_mix(
    combined_experience: str | Path,
    output_directory: str | Path,
    *,
    fractions: Iterable[float] = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
    seeds: Iterable[int] = (21, 42, 84),
    epochs: int = 700,
    progress=None,
) -> DQNDomainMixReport:
    """Select a Pagila DQN fraction on disjoint development templates."""
    import numpy as np

    from .control_benchmark import evaluate_dqn_control
    from .dqn import train_dqn
    from .dqn_features import GENERIC_V3_ACTION_ENCODING, LEGACY_ACTION_ENCODING

    fractions = sorted(set(float(value) for value in fractions))
    seeds = list(dict.fromkeys(int(value) for value in seeds))
    if not fractions or any(value < 0.0 or value > 1.0 for value in fractions):
        raise ValueError("fractions must contain values in the interval [0, 1]")
    if not seeds:
        raise ValueError("at least one seed is required")
    if epochs <= 0:
        raise ValueError("epochs must be positive")
    with Path(combined_experience).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    encodings = {
        record.get("action_encoding_version", LEGACY_ACTION_ENCODING)
        for record in records
    }
    if encodings != {GENERIC_V3_ACTION_ENCODING}:
        raise ValueError("DQN domain mix requires generic-v3 experience")
    observed = {infer_development_domain(record[GROUP_COLUMN]) for record in records}
    if observed != set(DEVELOPMENT_DOMAINS):
        raise ValueError(f"expected development domains {DEVELOPMENT_DOMAINS}, got {sorted(observed)}")

    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    runs: list[DQNMixRun] = []
    total = len(seeds) * len(fractions)
    position = 0
    for seed in seeds:
        outer_train, outer_holdout = _dqn_outer_template_split(records, seed)
        for fraction in fractions:
            position += 1
            selected_train = _select_dqn_pagila_fraction(
                outer_train,
                fraction=fraction,
                seed=seed,
            )
            run_name = f"fraction_{fraction:g}_seed_{seed}"
            run_directory = destination / "runs" / run_name
            training_path = run_directory / "training.jsonl"
            _write_jsonl(selected_train, training_path)
            if progress:
                progress(position, total, fraction, seed, "training")
            model_directory = run_directory / "model"
            train_dqn(
                training_path,
                model_directory,
                epochs=epochs,
                batch_size=64,
                learning_rate=1e-3,
                seed=seed,
                split_mode="unseen-template",
                ranking_weight=0.10,
                sampling_mode="group",
            )
            domain_metrics: list[DQNDomainMetric] = []
            for domain in DEVELOPMENT_DOMAINS:
                domain_records = [
                    record
                    for record in outer_holdout
                    if infer_development_domain(record[GROUP_COLUMN]) == domain
                ]
                holdout_path = run_directory / f"{domain}_holdout.jsonl"
                _write_jsonl(domain_records, holdout_path)
                metrics = evaluate_dqn_control(
                    holdout_path,
                    model_directory / "dqn_index_advisor.pt",
                    run_directory / f"{domain}_evaluation",
                    seed=seed,
                )
                domain_metrics.append(
                    DQNDomainMetric(
                        domain=domain,
                        query_count=metrics.query_count,
                        recommendation_accuracy=metrics.recommendation_accuracy,
                        mean_regret=metrics.mean_regret,
                    )
                )
            pagila_queries = {
                record["query_id"]
                for record in selected_train
                if infer_development_domain(record[GROUP_COLUMN]) == "pagila"
            }
            runs.append(
                DQNMixRun(
                    fraction=fraction,
                    seed=seed,
                    training_action_count=len(selected_train),
                    pagila_training_query_count=len(pagila_queries),
                    macro_accuracy=float(
                        np.mean(
                            [metric.recommendation_accuracy for metric in domain_metrics]
                        )
                    ),
                    macro_mean_regret=float(
                        np.mean([metric.mean_regret for metric in domain_metrics])
                    ),
                    domains=domain_metrics,
                )
            )
            if progress:
                progress(position, total, fraction, seed, "complete")

    summaries: list[DQNFractionSummary] = []
    for fraction in fractions:
        matching = [run for run in runs if run.fraction == fraction]
        summaries.append(
            DQNFractionSummary(
                fraction=fraction,
                mean_macro_accuracy=float(
                    np.mean([run.macro_accuracy for run in matching])
                ),
                mean_macro_regret=float(
                    np.mean([run.macro_mean_regret for run in matching])
                ),
                worst_macro_regret=max(run.macro_mean_regret for run in matching),
            )
        )
    selected = min(
        summaries,
        key=lambda item: (
            item.mean_macro_regret,
            item.worst_macro_regret,
            -item.mean_macro_accuracy,
            item.fraction,
        ),
    )
    report = DQNDomainMixReport(
        selection_protocol="development-only unseen-template macro mean regret",
        fractions=fractions,
        seeds=seeds,
        runs=runs,
        summaries=summaries,
        selected_fraction=selected.fraction,
    )
    (destination / "dqn_domain_mix_report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_summary_csv(summaries, destination / "dqn_domain_mix_summary.csv")
    final_records = _select_dqn_pagila_fraction(
        records,
        fraction=selected.fraction,
        seed=seeds[0],
    )
    _write_jsonl(final_records, destination / "selected_dqn_training.jsonl")
    return report


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


def _write_summary_csv(
    summaries: list[FractionSummary] | list[DQNFractionSummary],
    path: Path,
) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(summaries[0])))
        writer.writeheader()
        writer.writerows(asdict(summary) for summary in summaries)


def _dqn_outer_template_split(
    records: list[dict],
    seed: int,
) -> tuple[list[dict], list[dict]]:
    holdout_templates: set[str] = set()
    for domain in DEVELOPMENT_DOMAINS:
        templates = sorted(
            {
                record[GROUP_COLUMN]
                for record in records
                if infer_development_domain(record[GROUP_COLUMN]) == domain
            }
        )
        randomizer = random.Random(f"dqn-template-split:{seed}:{domain}")
        randomizer.shuffle(templates)
        holdout_count = max(1, math.ceil(len(templates) * 0.20))
        holdout_templates.update(templates[:holdout_count])
    return (
        [record for record in records if record[GROUP_COLUMN] not in holdout_templates],
        [record for record in records if record[GROUP_COLUMN] in holdout_templates],
    )


def _select_dqn_pagila_fraction(
    records: list[dict],
    *,
    fraction: float,
    seed: int,
) -> list[dict]:
    selected_query_ids: set[str] = {
        record["query_id"]
        for record in records
        if infer_development_domain(record[GROUP_COLUMN]) != "pagila"
    }
    if fraction > 0.0:
        query_ids_by_template: dict[str, list[str]] = {}
        for record in records:
            if infer_development_domain(record[GROUP_COLUMN]) != "pagila":
                continue
            values = query_ids_by_template.setdefault(record[GROUP_COLUMN], [])
            if record["query_id"] not in values:
                values.append(record["query_id"])
        for template, query_ids in sorted(query_ids_by_template.items()):
            query_ids = list(query_ids)
            randomizer = random.Random(f"dqn-pagila-fraction:{seed}:{template}")
            randomizer.shuffle(query_ids)
            count = max(1, math.ceil(len(query_ids) * fraction))
            selected_query_ids.update(query_ids[:count])
    return [record for record in records if record["query_id"] in selected_query_ids]


def _write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
