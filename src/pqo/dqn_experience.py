"""Collection of real PostgreSQL rewards for DQN training."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
from typing import Callable, Iterable

from .dqn_features import build_query_state, encode_action
from .config import DatabaseSettings
from .index_actions import IndexAction, IndexActionKind, generate_index_actions
from .index_environment import IndexExperimentEnvironment
from .query_generator import AviationQueryGenerator
from .query_case import QueryCase


def collect_dqn_experience(
    count: int,
    output_path: str | Path,
    *,
    actions_per_query: int = 1,
    repetitions: int = 1,
    seed: int = 42,
    resume: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    connection=None,
) -> int:
    if count <= 0 or actions_per_query <= 0:
        raise ValueError("count and actions_per_query must be positive")

    if connection is None:
        import psycopg

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return collect_dqn_experience(
                count,
                output_path,
                actions_per_query=actions_per_query,
                repetitions=repetitions,
                seed=seed,
                resume=resume,
                progress=progress,
                connection=owned_connection,
            )

    generator = AviationQueryGenerator(seed=seed)
    environment = IndexExperimentEnvironment(repetitions=repetitions)
    randomizer = random.Random(seed)
    cases = generator.generate(count)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed: set[tuple[str, str]] = set()
    existing_count = 0
    if resume and destination.exists():
        with destination.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                completed.add((record["template_id"], record["query_id"]))
                existing_count += 1

    mode = "a" if resume else "w"
    new_count = 0
    with destination.open(mode, encoding="utf-8", newline="\n") as stream:
        for position, case in enumerate(cases, start=1):
            actions = generate_index_actions(case.sql_text)
            query_id = hashlib.sha256(case.sql_text.encode()).hexdigest()
            candidates = list(actions[1:])
            selected = randomizer.sample(
                candidates, min(actions_per_query, len(candidates))
            )
            if (case.template_id, query_id) in completed:
                if progress:
                    progress(position, len(cases), f"{case.template_id} (cached)")
                continue

            state = build_query_state(case.sql_text, connection=connection)
            query_records = [
                _record(
                    case.template_id,
                    query_id,
                    case.sql_text,
                    state,
                    actions[0],
                    None,
                )
            ]

            for action in selected:
                result = environment.evaluate(
                    case.sql_text, action, connection=connection
                )
                query_records.append(
                    _record(
                        case.template_id,
                        query_id,
                        case.sql_text,
                        state,
                        action,
                        result,
                    )
                )

            for record in query_records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            new_count += len(query_records)
            if progress:
                progress(position, len(cases), case.template_id)

    return existing_count + new_count


def collect_dqn_case_experience(
    cases: Iterable[QueryCase],
    output_path: str | Path,
    *,
    actions_per_query: int | None = None,
    repetitions: int = 1,
    seed: int = 42,
    resume: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    connection=None,
) -> int:
    """Collect rewards for an explicit holdout workload, optionally testing all actions."""
    cases = list(cases)
    if not cases:
        raise ValueError("At least one query case is required")
    if actions_per_query is not None and actions_per_query <= 0:
        raise ValueError("actions_per_query must be positive or None")
    if connection is None:
        import psycopg

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return collect_dqn_case_experience(
                cases,
                output_path,
                actions_per_query=actions_per_query,
                repetitions=repetitions,
                seed=seed,
                resume=resume,
                progress=progress,
                connection=owned_connection,
            )

    environment = IndexExperimentEnvironment(repetitions=repetitions)
    randomizer = random.Random(seed)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed: set[tuple[str, str]] = set()
    existing_count = 0
    if resume and destination.exists():
        with destination.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                completed.add((record["template_id"], record["query_id"]))
                existing_count += 1

    mode = "a" if resume else "w"
    new_count = 0
    with destination.open(mode, encoding="utf-8", newline="\n") as stream:
        for position, case in enumerate(cases, start=1):
            actions = generate_index_actions(case.sql_text)
            query_id = hashlib.sha256(case.sql_text.encode()).hexdigest()
            if (case.template_id, query_id) in completed:
                if progress:
                    progress(position, len(cases), f"{case.template_id} (cached)")
                continue
            candidates = list(actions[1:])
            selected = (
                candidates
                if actions_per_query is None
                else randomizer.sample(candidates, min(actions_per_query, len(candidates)))
            )
            state = build_query_state(case.sql_text, connection=connection)
            records = [
                _record(case.template_id, query_id, case.sql_text, state, actions[0], None)
            ]
            for action in selected:
                result = environment.evaluate(case.sql_text, action, connection=connection)
                records.append(
                    _record(case.template_id, query_id, case.sql_text, state, action, result)
                )
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            new_count += len(records)
            if progress:
                progress(position, len(cases), case.template_id)
    return existing_count + new_count


def _record(template_id, query_id, sql_text, state, action: IndexAction, result):
    action_data = asdict(action)
    if result is None:
        outcome = {
            "reward": 0.0,
            "baseline_time_ms": None,
            "candidate_time_ms": None,
            "improvement_ratio": 0.0,
            "candidate_plan_cost": None,
            "candidate_uses_index": False,
        }
    else:
        outcome = {
            "reward": result.reward,
            "baseline_time_ms": result.baseline_time_ms,
            "candidate_time_ms": result.candidate_time_ms,
            "improvement_ratio": result.improvement_ratio,
            "candidate_plan_cost": result.candidate_plan_cost,
            "candidate_uses_index": result.candidate_uses_index,
        }
    return {
        "template_id": template_id,
        "query_id": query_id,
        "sql_text": sql_text,
        "state": state,
        "action": action_data,
        "action_features": encode_action(action, sql_text),
        "done": True,
        **outcome,
    }


def refresh_action_features(path: str | Path) -> int:
    """Rebuild deterministic action vectors after feature-schema changes."""
    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    count = 0
    with destination.open(encoding="utf-8") as source, temporary.open(
        "w", encoding="utf-8", newline="\n"
    ) as target:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            data = record["action"]
            action = IndexAction(
                kind=IndexActionKind(data["kind"]),
                schema_name=data.get("schema_name"),
                table_name=data.get("table_name"),
                key_columns=tuple(data.get("key_columns") or ()),
                include_columns=tuple(data.get("include_columns") or ()),
            )
            record["action_features"] = encode_action(action, record["sql_text"])
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    temporary.replace(destination)
    return count
