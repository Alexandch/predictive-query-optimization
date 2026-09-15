"""Measured, transaction-safe sequential recommendations for the desktop app."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .config import DatabaseSettings
from .dqn_features import (
    SEQUENTIAL_ACTION_ENCODING,
    build_query_state,
    collect_action_database_context,
    encode_action,
    encode_sequential_state,
)
from .index_actions import IndexAction, IndexActionKind
from .sequential_dqn import predict_sequential_action_values
from .sequential_environment import SequentialIndexEnvironment


@dataclass(frozen=True, slots=True)
class SequentialRecommendationStep:
    action: IndexAction
    predicted_q: float
    measured_reward: float
    before_time_ms: float
    after_time_ms: float
    index_size_bytes: int
    creation_time_ms: float
    used_by_postgresql: bool


@dataclass(frozen=True, slots=True)
class SequentialRecommendationPlan:
    steps: tuple[SequentialRecommendationStep, ...]
    baseline_time_ms: float
    final_time_ms: float
    storage_budget_bytes: int
    used_budget_bytes: int
    candidate_count: int
    decision_threshold: float
    minimum_baseline_time_ms: float
    minimum_absolute_improvement_ms: float
    terminal_reason: str
    query_run_id: int | None = None
    sequential_analysis_id: int | None = None

    @property
    def measured_improvement_ratio(self) -> float:
        return (self.baseline_time_ms - self.final_time_ms) / max(
            self.baseline_time_ms, 0.001
        )


def recommend_sequential_indexes(
    sql_text: str,
    model_path: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    max_steps: int = 2,
    storage_budget_bytes: int = 64 * 1024 * 1024,
    repetitions: int = 1,
    minimum_baseline_time_ms: float = 50.0,
    minimum_absolute_improvement_ms: float = 5.0,
    persist: bool = False,
    query_run_id: int | None = None,
) -> SequentialRecommendationPlan:
    """Measure a model-selected plan and discard all trial indexes on exit."""
    import psycopg
    import torch

    if minimum_baseline_time_ms < 0 or minimum_absolute_improvement_ms < 0:
        raise ValueError("Recommendation time thresholds must be non-negative")
    settings = settings or DatabaseSettings.from_env()
    resolved_model = str(Path(model_path).resolve())
    artifact = torch.load(resolved_model, map_location="cpu", weights_only=True)
    if artifact.get("action_encoding_version") != SEQUENTIAL_ACTION_ENCODING:
        raise ValueError("Select a generic-v4-sequential DQN model")
    threshold = float(artifact["decision_threshold"])
    environment = SequentialIndexEnvironment(
        settings,
        allowed_schemas=settings.allowed_schemas,
        repetitions=repetitions,
        max_steps=max_steps,
        storage_budget_bytes=storage_budget_bytes,
    )
    accepted_steps = []
    candidate_count = 0
    terminal_reason = "model_stop"
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        base_state = build_query_state(sql_text, connection=connection)
        with environment.episode(sql_text, connection=connection) as episode:
            baseline_time = episode.state.baseline_time_ms
            final_time = baseline_time
            used_budget = 0
            if baseline_time < minimum_baseline_time_ms:
                terminal_reason = "below_runtime_threshold"
            while not episode.state.done:
                if terminal_reason == "below_runtime_threshold":
                    break
                actions = list(episode.available_actions)
                candidate_count = max(candidate_count, len(actions))
                encoded_state = encode_sequential_state(base_state, episode.state)
                action_features = [
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
                values = predict_sequential_action_values(
                    resolved_model, encoded_state, action_features
                )
                create_indices = [
                    index
                    for index, action in enumerate(actions)
                    if action.kind is IndexActionKind.CREATE
                ]
                if not create_indices:
                    terminal_reason = "no_candidates"
                    break
                best_index = max(create_indices, key=values.__getitem__)
                if values[best_index] <= threshold:
                    terminal_reason = "model_stop"
                    break
                previous_time = episode.state.current_time_ms
                transition = episode.step(actions[best_index])
                if not transition.accepted:
                    terminal_reason = transition.terminal_reason or "rejected"
                    break
                if not transition.candidate_uses_created_index:
                    terminal_reason = "index_not_used"
                    break
                if transition.reward <= 0:
                    terminal_reason = "non_positive_reward"
                    break
                if (
                    previous_time - transition.next_state.current_time_ms
                    < minimum_absolute_improvement_ms
                ):
                    terminal_reason = "absolute_gain_too_small"
                    break
                accepted_steps.append(
                    SequentialRecommendationStep(
                        action=actions[best_index],
                        predicted_q=float(values[best_index]),
                        measured_reward=transition.reward,
                        before_time_ms=previous_time,
                        after_time_ms=transition.next_state.current_time_ms,
                        index_size_bytes=transition.index_size_bytes,
                        creation_time_ms=transition.creation_time_ms,
                        used_by_postgresql=transition.candidate_uses_created_index,
                    )
                )
                final_time = transition.next_state.current_time_ms
                used_budget = transition.next_state.used_budget_bytes
                if transition.next_state.done:
                    terminal_reason = transition.terminal_reason or "max_steps"
                    break
    plan = SequentialRecommendationPlan(
        steps=tuple(accepted_steps),
        baseline_time_ms=baseline_time,
        final_time_ms=final_time,
        storage_budget_bytes=storage_budget_bytes,
        used_budget_bytes=used_budget,
        candidate_count=candidate_count,
        decision_threshold=threshold,
        minimum_baseline_time_ms=minimum_baseline_time_ms,
        minimum_absolute_improvement_ms=minimum_absolute_improvement_ms,
        terminal_reason=terminal_reason,
    )
    if persist:
        from .repository import save_sequential_analysis

        saved_query_run_id, sequential_analysis_id = save_sequential_analysis(
            sql_text,
            plan,
            settings=settings,
            query_run_id=query_run_id,
            model_version=Path(model_path).name,
        )
        plan = replace(
            plan,
            query_run_id=saved_query_run_id,
            sequential_analysis_id=sequential_analysis_id,
        )
    return plan
