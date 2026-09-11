"""End-to-end rollouts for a frozen sequential index policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import json
from pathlib import Path
import random

from .config import DatabaseSettings
from .dqn_features import (
    SEQUENTIAL_ACTION_ENCODING,
    build_query_state,
    collect_action_database_context,
    encode_action,
    encode_sequential_state,
)
from .index_actions import IndexActionKind
from .sequential_dqn import predict_sequential_action_values
from .sequential_environment import SequentialIndexEnvironment


@dataclass(frozen=True, slots=True)
class SequentialRolloutMetrics:
    episode_count: int
    template_count: int
    model_mean_return: float
    random_mean_return: float
    model_positive_rate: float
    random_positive_rate: float
    model_negative_rate: float
    random_negative_rate: float
    model_stop_rate: float
    random_stop_rate: float
    model_mean_create_actions: float
    random_mean_create_actions: float
    model_index_use_rate: float
    random_index_use_rate: float
    model_budget_rejections: int
    random_budget_rejections: int
    decision_threshold: float
    gamma: float
    random_seed: int


def evaluate_sequential_rollout(
    experience_path: str | Path,
    model_path: str | Path,
    output_dir: str | Path,
    *,
    allowed_schemas: frozenset[str],
    settings: DatabaseSettings | None = None,
    repetitions: int = 1,
    max_steps: int = 2,
    storage_budget_bytes: int = 64 * 1024 * 1024,
    random_seed: int = 42,
) -> SequentialRolloutMetrics:
    """Run model and random policies against PostgreSQL with rollback per run."""
    import psycopg
    import torch

    settings = settings or DatabaseSettings.from_env()
    artifact = torch.load(
        str(Path(model_path).resolve()), map_location="cpu", weights_only=True
    )
    if artifact.get("action_encoding_version") != SEQUENTIAL_ACTION_ENCODING:
        raise ValueError("Rollout requires a generic-v4-sequential model")
    threshold = float(artifact["decision_threshold"])
    gamma = float(artifact["gamma"])
    cases = _unique_cases(experience_path)
    environment = SequentialIndexEnvironment(
        settings,
        allowed_schemas=allowed_schemas,
        repetitions=repetitions,
        max_steps=max_steps,
        storage_budget_bytes=storage_budget_bytes,
    )
    randomizer = random.Random(random_seed)
    rows = []
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        for position, case in enumerate(cases):
            base_state = build_query_state(case["sql_text"], connection=connection)
            policies = ("model", "random") if position % 2 == 0 else ("random", "model")
            results = {}
            for policy in policies:
                results[policy] = _run_episode(
                    environment,
                    case["sql_text"],
                    base_state,
                    model_path,
                    threshold,
                    gamma,
                    connection,
                    randomizer if policy == "random" else None,
                )
            for policy in ("model", "random"):
                rows.append(
                    {
                        "episode_id": case["episode_id"],
                        "template_id": case["template_id"],
                        "policy": policy,
                        **results[policy],
                    }
                )
    metrics = _rollout_metrics(rows, threshold, gamma, random_seed)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "sequential_rollout_metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (destination / "sequential_rollout_episodes.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return metrics


def _run_episode(
    environment,
    sql_text,
    base_state,
    model_path,
    threshold,
    gamma,
    connection,
    randomizer,
):
    total_return = 0.0
    create_count = used_count = budget_rejections = 0
    stopped = False
    selected = []
    with environment.episode(sql_text, connection=connection) as episode:
        while not episode.state.done:
            actions = list(episode.available_actions)
            if randomizer is None:
                state = encode_sequential_state(base_state, episode.state)
                features = [
                    encode_action(
                        action,
                        sql_text,
                        encoding_version=SEQUENTIAL_ACTION_ENCODING,
                        database_context=collect_action_database_context(
                            action, connection=connection
                        ),
                    )
                    for action in actions
                ]
                values = predict_sequential_action_values(model_path, state, features)
                action = _choose_model_action(actions, values, threshold)
            else:
                stop = next(
                    action for action in actions if action.kind is IndexActionKind.STOP
                )
                creates = [
                    action
                    for action in actions
                    if action.kind is IndexActionKind.CREATE
                ]
                action = (
                    stop
                    if not creates or randomizer.random() < 0.5
                    else randomizer.choice(creates)
                )
            if action.kind is IndexActionKind.STOP:
                episode.step(action)
                stopped = True
                break
            transition = episode.step(action)
            total_return += (gamma**create_count) * transition.reward
            create_count += 1
            used_count += int(transition.candidate_uses_created_index)
            budget_rejections += int(transition.terminal_reason == "budget_exceeded")
            selected.append(json.dumps(asdict(action), ensure_ascii=False))
            if not transition.accepted:
                break
    return {
        "discounted_return": total_return,
        "create_actions": create_count,
        "used_indexes": used_count,
        "budget_rejections": budget_rejections,
        "stopped": stopped,
        "selected_actions": " | ".join(selected),
    }


def _choose_model_action(actions, values, threshold):
    stop = next(action for action in actions if action.kind is IndexActionKind.STOP)
    creates = [
        (action, values[index])
        for index, action in enumerate(actions)
        if action.kind is IndexActionKind.CREATE
    ]
    if not creates:
        return stop
    best_action, best_value = max(creates, key=lambda item: item[1])
    return best_action if best_value > threshold else stop


def _unique_cases(path: str | Path) -> list[dict]:
    cases = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            cases.setdefault(
                record["episode_id"],
                {
                    "episode_id": record["episode_id"],
                    "template_id": record["template_id"],
                    "sql_text": record["sql_text"],
                },
            )
    if not cases:
        raise ValueError("Rollout experience contains no episodes")
    return list(cases.values())


def _rollout_metrics(rows, threshold, gamma, random_seed):
    policies = {
        name: [row for row in rows if row["policy"] == name]
        for name in ("model", "random")
    }

    def mean(name, field):
        values = policies[name]
        return sum(float(row[field]) for row in values) / len(values)

    def rate(name, predicate):
        values = policies[name]
        return sum(bool(predicate(row)) for row in values) / len(values)

    def index_use_rate(name):
        created = sum(int(row["create_actions"]) for row in policies[name])
        used = sum(int(row["used_indexes"]) for row in policies[name])
        return used / created if created else 0.0

    return SequentialRolloutMetrics(
        episode_count=len(policies["model"]),
        template_count=len({row["template_id"] for row in policies["model"]}),
        model_mean_return=mean("model", "discounted_return"),
        random_mean_return=mean("random", "discounted_return"),
        model_positive_rate=rate("model", lambda row: row["discounted_return"] > 0),
        random_positive_rate=rate("random", lambda row: row["discounted_return"] > 0),
        model_negative_rate=rate("model", lambda row: row["discounted_return"] < 0),
        random_negative_rate=rate("random", lambda row: row["discounted_return"] < 0),
        model_stop_rate=rate("model", lambda row: row["stopped"]),
        random_stop_rate=rate("random", lambda row: row["stopped"]),
        model_mean_create_actions=mean("model", "create_actions"),
        random_mean_create_actions=mean("random", "create_actions"),
        model_index_use_rate=index_use_rate("model"),
        random_index_use_rate=index_use_rate("random"),
        model_budget_rejections=sum(
            int(row["budget_rejections"]) for row in policies["model"]
        ),
        random_budget_rejections=sum(
            int(row["budget_rejections"]) for row in policies["random"]
        ),
        decision_threshold=threshold,
        gamma=gamma,
        random_seed=random_seed,
    )
