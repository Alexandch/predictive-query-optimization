"""Collect offline transitions for the sequential multi-index agent."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
from typing import Callable, Iterable

from .config import DatabaseSettings
from .dqn_features import (
    SEQUENTIAL_ACTION_ENCODING,
    build_query_state,
    collect_action_database_context,
    encode_action,
    encode_sequential_state,
)
from .index_actions import IndexAction
from .query_case import QueryCase
from .sequential_environment import SequentialIndexEnvironment


def collect_sequential_experience(
    cases: Iterable[QueryCase],
    output_path: str | Path,
    *,
    allowed_schemas: frozenset[str],
    seed: int = 42,
    repetitions: int = 1,
    max_steps: int = 2,
    storage_budget_bytes: int = 64 * 1024 * 1024,
    resume: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Collect STOP plus one behavior-policy CREATE transition per state."""
    import psycopg

    cases = list(cases)
    if not cases:
        raise ValueError("At least one query case is required")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = _prepare_resume(destination) if resume else set()
    settings = DatabaseSettings.from_env()
    environment = SequentialIndexEnvironment(
        settings,
        allowed_schemas=allowed_schemas,
        repetitions=repetitions,
        max_steps=max_steps,
        storage_budget_bytes=storage_budget_bytes,
    )
    randomizer = random.Random(seed)
    mode = "a" if resume else "w"
    written = 0
    with psycopg.connect(**settings.connection_kwargs()) as connection, destination.open(
        mode,
        encoding="utf-8",
        newline="\n",
    ) as stream:
        for position, case in enumerate(cases, start=1):
            episode_id = hashlib.sha256(
                f"{case.template_id}|{case.sql_text}".encode()
            ).hexdigest()
            if episode_id in completed:
                if progress:
                    progress(position, len(cases), f"{case.template_id} (cached)")
                continue
            base_state = build_query_state(case.sql_text, connection=connection)
            with environment.episode(case.sql_text, connection=connection) as episode:
                while not episode.state.done:
                    state_id = _state_id(episode_id, episode.state.selected_actions)
                    encoded_state = encode_sequential_state(base_state, episode.state)
                    stop = IndexAction.stop()
                    create_actions = [
                        action
                        for action in episode.available_actions
                        if action != stop
                    ]
                    stream.write(
                        json.dumps(
                            _record(
                                episode_id=episode_id,
                                state_id=state_id,
                                template_id=case.template_id,
                                sql_text=case.sql_text,
                                state=encoded_state,
                                action=stop,
                                action_features=encode_action(
                                    stop,
                                    case.sql_text,
                                    encoding_version=SEQUENTIAL_ACTION_ENCODING,
                                ),
                                reward=0.0,
                                next_state=None,
                                next_action_features=[],
                                done=True,
                                behavior_done=not create_actions,
                                accepted=True,
                                terminal_reason="stop",
                                index_size_bytes=0,
                                creation_time_ms=0.0,
                                candidate_uses_created_index=False,
                                candidate_uses_selected_index=False,
                            ),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    written += 1
                    if not create_actions:
                        break
                    action = randomizer.choice(create_actions)
                    action_context = collect_action_database_context(
                        action,
                        connection=connection,
                    )
                    action_features = encode_action(
                        action,
                        case.sql_text,
                        encoding_version=SEQUENTIAL_ACTION_ENCODING,
                        database_context=action_context,
                    )
                    transition = episode.step(action)
                    next_state = encode_sequential_state(base_state, transition.next_state)
                    next_features = []
                    if transition.accepted and not transition.next_state.done:
                        for next_action in episode.available_actions:
                            context = collect_action_database_context(
                                next_action,
                                connection=connection,
                            )
                            next_features.append(
                                encode_action(
                                    next_action,
                                    case.sql_text,
                                    encoding_version=SEQUENTIAL_ACTION_ENCODING,
                                    database_context=context,
                                )
                            )
                    stream.write(
                        json.dumps(
                            _record(
                                episode_id=episode_id,
                                state_id=state_id,
                                template_id=case.template_id,
                                sql_text=case.sql_text,
                                state=encoded_state,
                                action=action,
                                action_features=action_features,
                                reward=transition.reward,
                                next_state=next_state,
                                next_action_features=next_features,
                                done=(
                                    transition.next_state.done
                                    or not transition.accepted
                                ),
                                behavior_done=(
                                    transition.next_state.done
                                    or not transition.accepted
                                ),
                                accepted=transition.accepted,
                                terminal_reason=transition.terminal_reason,
                                index_size_bytes=transition.index_size_bytes,
                                creation_time_ms=transition.creation_time_ms,
                                candidate_uses_created_index=(
                                    transition.candidate_uses_created_index
                                ),
                                candidate_uses_selected_index=(
                                    transition.candidate_uses_selected_index
                                ),
                            ),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    written += 1
                    stream.flush()
                    if not transition.accepted:
                        break
            if progress:
                progress(position, len(cases), case.template_id)
    return written


def _record(**values) -> dict:
    action = values.pop("action")
    return {
        "format_version": 1,
        "action_encoding_version": SEQUENTIAL_ACTION_ENCODING,
        "action": asdict(action),
        **values,
    }


def _state_id(episode_id: str, actions: tuple[IndexAction, ...]) -> str:
    selected = "|".join(str(action) for action in actions)
    return hashlib.sha256(f"{episode_id}|{selected}".encode()).hexdigest()


def _completed_episode_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open(encoding="utf-8") as stream:
        completed = set()
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("behavior_done"):
                completed.add(record["episode_id"])
        return completed


def _prepare_resume(path: Path) -> set[str]:
    """Discard an interrupted trajectory before appending deterministic replay."""
    completed = _completed_episode_ids(path)
    if not path.is_file():
        return completed
    with path.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    kept = [record for record in records if record["episode_id"] in completed]
    temporary = path.with_suffix(path.suffix + ".resume.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for record in kept:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(path)
    return completed
