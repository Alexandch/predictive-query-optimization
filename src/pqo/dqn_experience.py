"""Collection of real PostgreSQL rewards for DQN training."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
from typing import Callable

from .dqn_features import build_query_state, encode_action
from .index_actions import IndexAction, generate_index_actions
from .index_environment import IndexExperimentEnvironment
from .query_generator import AviationQueryGenerator


def collect_dqn_experience(
    count: int,
    output_path: str | Path,
    *,
    actions_per_query: int = 1,
    repetitions: int = 1,
    seed: int = 42,
    resume: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    if count <= 0 or actions_per_query <= 0:
        raise ValueError("count and actions_per_query must be positive")

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

            state = build_query_state(case.sql_text)
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
                result = environment.evaluate(case.sql_text, action)
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
        "action_features": encode_action(action),
        "done": True,
        **outcome,
    }
