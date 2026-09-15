"""Measured training data and classifier for optimization-strategy selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Callable, Iterable

from .config import DatabaseSettings
from .dqn_features import (
    STATE_FEATURE_NAMES,
    build_query_state,
    collect_action_database_context,
)
from .index_actions import IndexAction, generate_index_actions
from .index_environment import IndexExperimentEnvironment, IndexExperimentResult
from .query_case import QueryCase
from .sql_rewrite import SQLRewritePlan, evaluate_sql_rewrites, generate_sql_rewrites


TRAINING_SCHEMAS = frozenset({"aviation", "retail", "pagila"})
SEALED_CONTROL_SCHEMAS = frozenset({"logistics", "chbenchmark"})
REWRITE_RULE_IDS = (
    "count-positive-to-exists",
    "or-equality-to-in",
    "range-to-between",
)
STRATEGY_FEATURE_NAMES = (
    *STATE_FEATURE_NAMES,
    "index_candidate_count_log",
    "rewrite_candidate_count",
    *(f"rewrite_rule_{rule_id}" for rule_id in REWRITE_RULE_IDS),
)


class StrategyKind(StrEnum):
    NOOP = "NOOP"
    CREATE_INDEX = "CREATE_INDEX"
    REWRITE_QUERY = "REWRITE_QUERY"


@dataclass(frozen=True, slots=True)
class StrategyTrainingMetrics:
    sample_count: int
    train_count: int
    test_count: int
    train_templates: list[str]
    test_templates: list[str]
    class_counts: dict[str, int]
    accuracy: float
    balanced_accuracy: float
    macro_f1: float
    majority_accuracy: float
    confusion_matrix: list[list[int]]
    labels: list[str]
    seed: int


def build_strategy_features(
    sql_text: str,
    query_state: list[float],
    index_candidate_count: int,
) -> list[float]:
    """Append available-action signals to the ordinary query state."""
    if len(query_state) != len(STATE_FEATURE_NAMES):
        raise ValueError("Query-state length does not match the feature contract")
    if index_candidate_count < 0:
        raise ValueError("index_candidate_count must be non-negative")
    rules = {candidate.rule_id for candidate in generate_sql_rewrites(sql_text)}
    return [
        *map(float, query_state),
        math.log1p(index_candidate_count),
        float(len(rules)),
        *(float(rule_id in rules) for rule_id in REWRITE_RULE_IDS),
    ]


def choose_strategy_label(
    index_results: Iterable[IndexExperimentResult],
    rewrite_plan: SQLRewritePlan,
    *,
    minimum_reward: float = 0.05,
    minimum_absolute_improvement_ms: float = 5.0,
) -> StrategyKind:
    """Choose the best material, verified outcome using comparable ratio rewards."""
    if minimum_reward < 0 or minimum_absolute_improvement_ms < 0:
        raise ValueError("Strategy thresholds must be non-negative")

    best_kind = StrategyKind.NOOP
    best_reward = 0.0
    for result in index_results:
        absolute_gain = result.baseline_time_ms - result.candidate_time_ms
        if (
            result.candidate_uses_index
            and result.reward >= minimum_reward
            and absolute_gain >= minimum_absolute_improvement_ms
            and result.reward > best_reward
        ):
            best_kind = StrategyKind.CREATE_INDEX
            best_reward = result.reward

    for evaluation in rewrite_plan.evaluations:
        ratio = evaluation.improvement_ratio
        absolute_gain = evaluation.absolute_improvement_ms
        if (
            evaluation.equivalent
            and ratio is not None
            and absolute_gain is not None
            and ratio >= minimum_reward
            and absolute_gain >= minimum_absolute_improvement_ms
            and ratio > best_reward
        ):
            best_kind = StrategyKind.REWRITE_QUERY
            best_reward = ratio
    return best_kind


def collect_strategy_experience(
    cases: Iterable[QueryCase],
    output_path: str | Path,
    *,
    actions_per_query: int | None = 6,
    repetitions: int = 1,
    seed: int = 42,
    resume: bool = False,
    allowed_schemas: frozenset[str] = TRAINING_SCHEMAS,
    minimum_reward: float = 0.05,
    minimum_absolute_improvement_ms: float = 5.0,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Measure index and rewrite outcomes and persist one labeled row per SQL."""
    cases = list(cases)
    if not cases:
        raise ValueError("At least one query case is required")
    if actions_per_query is not None and actions_per_query <= 0:
        raise ValueError("actions_per_query must be positive or None")
    if allowed_schemas & SEALED_CONTROL_SCHEMAS:
        raise ValueError("Sealed logistics/CH control schemas cannot be used for training")
    if not allowed_schemas or not allowed_schemas <= TRAINING_SCHEMAS:
        raise ValueError("Training schemas must be a subset of aviation, retail and pagila")
    for case in cases:
        _assert_training_case_schemas(case.sql_text, allowed_schemas)

    import psycopg

    settings = DatabaseSettings.from_env()
    environment = IndexExperimentEnvironment(
        settings,
        repetitions=repetitions,
        allowed_schemas=allowed_schemas,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed: set[str] = set()
    existing_count = 0
    if resume and destination.exists():
        with destination.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    record = json.loads(line)
                    completed.add(str(record["query_id"]))
                    existing_count += 1

    randomizer = random.Random(seed)
    mode = "a" if resume else "w"
    new_count = 0
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with destination.open(mode, encoding="utf-8", newline="\n") as stream:
            for position, case in enumerate(cases, start=1):
                query_id = hashlib.sha256(case.sql_text.encode()).hexdigest()
                if query_id in completed:
                    if progress:
                        progress(position, len(cases), f"{case.template_id} (cached)")
                    continue

                generated_actions = list(
                    generate_index_actions(
                        case.sql_text,
                        allowed_schemas=allowed_schemas,
                    )[1:]
                )
                action_contexts = {
                    action: collect_action_database_context(
                        action, connection=connection
                    )
                    for action in generated_actions
                }
                all_actions = [
                    action
                    for action in generated_actions
                    if not action_contexts[action]["exact_index_exists"]
                    and not action_contexts[action]["prefix_index_exists"]
                ]
                selected_actions = (
                    all_actions
                    if actions_per_query is None
                    else randomizer.sample(
                        all_actions, min(actions_per_query, len(all_actions))
                    )
                )
                state = build_query_state(case.sql_text, connection=connection)
                index_results = [
                    environment.evaluate(case.sql_text, action, connection=connection)
                    for action in selected_actions
                ]
                rewrite_plan = evaluate_sql_rewrites(
                    case.sql_text,
                    settings,
                    repetitions=repetitions,
                    minimum_baseline_time_ms=0.0,
                    minimum_absolute_improvement_ms=0.0,
                    minimum_improvement_ratio=0.0,
                )
                label = choose_strategy_label(
                    index_results,
                    rewrite_plan,
                    minimum_reward=minimum_reward,
                    minimum_absolute_improvement_ms=minimum_absolute_improvement_ms,
                )
                record = {
                    "format_version": 1,
                    "template_id": case.template_id,
                    "query_id": query_id,
                    "sql_text": case.sql_text,
                    "state": state,
                    "strategy_features": build_strategy_features(
                        case.sql_text, state, len(all_actions)
                    ),
                    "strategy_feature_names": list(STRATEGY_FEATURE_NAMES),
                    "label": label.value,
                    "label_policy": {
                        "minimum_reward": minimum_reward,
                        "minimum_absolute_improvement_ms": (
                            minimum_absolute_improvement_ms
                        ),
                    },
                    "index_candidates": [
                        _index_result_payload(result, action_contexts[result.action])
                        for result in index_results
                    ],
                    "generated_index_candidate_count": len(generated_actions),
                    "eligible_index_candidate_count": len(all_actions),
                    "rewrite_plan": asdict(rewrite_plan),
                }
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                new_count += 1
                if progress:
                    progress(position, len(cases), case.template_id)
    return existing_count + new_count


def train_strategy_classifier(
    experience_path: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 42,
) -> StrategyTrainingMetrics:
    """Train an XGBoost strategy selector with an unseen-template test split."""
    import joblib
    import numpy as np
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        confusion_matrix,
        f1_score,
    )
    from sklearn.model_selection import StratifiedGroupKFold
    from xgboost import XGBClassifier

    records = _read_strategy_records(experience_path)
    labels = [kind.value for kind in StrategyKind]
    class_to_index = {label: index for index, label in enumerate(labels)}
    class_counts = {label: sum(r["label"] == label for r in records) for label in labels}
    templates = {str(record["template_id"]) for record in records}
    if len(records) < 30 or len(templates) < 6:
        raise ValueError("Strategy training requires at least 30 queries and 6 templates")
    if min(class_counts.values()) < 3:
        raise ValueError("Strategy training requires at least 3 queries in every class")

    x = np.asarray([record["strategy_features"] for record in records], dtype=np.float32)
    y = np.asarray([class_to_index[record["label"]] for record in records])
    groups = np.asarray([str(record["template_id"]) for record in records])
    splitter = StratifiedGroupKFold(n_splits=3, shuffle=True, random_state=seed)
    split = next(
        (
            (train, test)
            for train, test in splitter.split(x, y, groups)
            if len(set(y[train])) == len(labels) and len(set(y[test])) == len(labels)
        ),
        None,
    )
    if split is None:
        raise ValueError(
            "Templates cannot form an unseen-template split containing every class"
        )
    train_indices, test_indices = split
    classifier = XGBClassifier(
        objective="multi:softprob",
        num_class=len(labels),
        n_estimators=300,
        max_depth=4,
        learning_rate=0.04,
        min_child_weight=2,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=1.0,
        random_state=seed,
        n_jobs=4,
        eval_metric="mlogloss",
    )
    classifier.fit(
        x[train_indices],
        y[train_indices],
        sample_weight=_balanced_sample_weights(y[train_indices], np, len(labels)),
    )
    predictions = classifier.predict(x[test_indices]).astype(int)
    majority_class = int(np.bincount(y[train_indices]).argmax())
    test_y = y[test_indices]
    metrics = StrategyTrainingMetrics(
        sample_count=len(records),
        train_count=len(train_indices),
        test_count=len(test_indices),
        train_templates=sorted(set(groups[train_indices])),
        test_templates=sorted(set(groups[test_indices])),
        class_counts=class_counts,
        accuracy=float(accuracy_score(test_y, predictions)),
        balanced_accuracy=float(balanced_accuracy_score(test_y, predictions)),
        macro_f1=float(f1_score(test_y, predictions, average="macro")),
        majority_accuracy=float(accuracy_score(test_y, [majority_class] * len(test_y))),
        confusion_matrix=confusion_matrix(
            test_y, predictions, labels=range(len(labels))
        ).tolist(),
        labels=labels,
        seed=seed,
    )

    classifier.fit(
        x,
        y,
        sample_weight=_balanced_sample_weights(y, np, len(labels)),
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "classifier": classifier,
            "feature_names": list(STRATEGY_FEATURE_NAMES),
            "labels": labels,
            "format_version": 1,
            "seed": seed,
            "class_balanced": True,
        },
        destination / "strategy_selector.joblib",
    )
    (destination / "metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metrics


def predict_strategy(model_path: str | Path, strategy_features: list[float]):
    """Return the selected strategy and probabilities for every strategy."""
    import joblib
    import numpy as np

    artifact = joblib.load(model_path)
    feature_names = artifact["feature_names"]
    if len(strategy_features) != len(feature_names):
        raise ValueError("Strategy feature count does not match the model artifact")
    probabilities = artifact["classifier"].predict_proba(
        np.asarray([strategy_features], dtype=np.float32)
    )[0]
    labels = artifact["labels"]
    best = int(np.argmax(probabilities))
    return {
        "strategy": labels[best],
        "probabilities": {
            label: float(probability)
            for label, probability in zip(labels, probabilities, strict=True)
        },
    }


def summarize_strategy_experience(path: str | Path) -> dict:
    records = _read_strategy_records(path)
    labels = {kind.value: 0 for kind in StrategyKind}
    for record in records:
        labels[record["label"]] += 1
    return {
        "sample_count": len(records),
        "template_count": len({record["template_id"] for record in records}),
        "class_counts": labels,
    }


def _read_strategy_records(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    if not records:
        raise ValueError("Strategy experience is empty")
    expected_features = list(STRATEGY_FEATURE_NAMES)
    for record in records:
        if record.get("label") not in {kind.value for kind in StrategyKind}:
            raise ValueError("Strategy experience contains an unsupported label")
        if record.get("strategy_feature_names") != expected_features:
            raise ValueError("Strategy experience uses an incompatible feature contract")
        if len(record.get("strategy_features", ())) != len(expected_features):
            raise ValueError("Strategy experience contains an invalid feature vector")
    return records


def _balanced_sample_weights(y, np, class_count: int):
    counts = np.bincount(y, minlength=class_count)
    return np.asarray(
        [len(y) / (class_count * counts[class_id]) for class_id in y],
        dtype=np.float32,
    )


def _index_result_payload(
    result: IndexExperimentResult,
    database_context: dict[str, int | float | bool],
) -> dict:
    payload = asdict(result)
    payload["action"]["kind"] = result.action.kind.value
    payload["action_database_context"] = database_context
    return payload


def _assert_training_case_schemas(
    sql_text: str, allowed_schemas: frozenset[str]
) -> None:
    from sqlglot import exp, parse_one

    tree = parse_one(sql_text, read="postgres")
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    schemas = {
        table.db or "public"
        for table in tree.find_all(exp.Table)
        if table.name not in cte_names
    }
    if not schemas or not schemas <= allowed_schemas:
        raise ValueError(
            "Every physical table in strategy training must belong to an allowed "
            "training schema"
        )
