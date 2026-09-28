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
from .sequential_dqn import (
    predict_sequential_action_values,
    sequential_inference_metadata,
)
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
    target_relation_rows: float = 0.0
    target_relation_size_bytes: int = 0
    target_existing_index_count: int = 0
    target_insert_count: int = 0
    target_update_count: int = 0
    target_delete_count: int = 0


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
    minimum_improvement_ratio: float = 0.05
    baseline_samples_ms: tuple[float, ...] = ()

    @property
    def baseline_min_time_ms(self) -> float:
        return min(self.baseline_samples_ms, default=self.baseline_time_ms)

    @property
    def baseline_max_time_ms(self) -> float:
        return max(self.baseline_samples_ms, default=self.baseline_time_ms)

    @property
    def measured_improvement_ratio(self) -> float:
        return (self.baseline_time_ms - self.final_time_ms) / max(
            self.baseline_time_ms, 0.001
        )


def _should_measure_candidate(
    predicted_q: float,
    decision_threshold: float,
    accepted_step_count: int,
) -> bool:
    """Always verify one best candidate during an explicit deep analysis."""
    return predicted_q > decision_threshold or accepted_step_count == 0


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
    minimum_improvement_ratio: float = 0.05,
    persist: bool = False,
    query_run_id: int | None = None,
) -> SequentialRecommendationPlan:
    """Measure a model-selected plan and discard all trial indexes on exit."""
    import psycopg

    if min(
        minimum_baseline_time_ms,
        minimum_absolute_improvement_ms,
        minimum_improvement_ratio,
    ) < 0:
        raise ValueError("Recommendation time thresholds must be non-negative")
    settings = settings or DatabaseSettings.from_env()
    resolved_model = str(Path(model_path).resolve())
    artifact = sequential_inference_metadata(resolved_model)
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
                generated_actions = list(episode.available_actions)
                encoded_state = encode_sequential_state(base_state, episode.state)
                generated_contexts = [
                    collect_action_database_context(action, connection=connection)
                    for action in generated_actions
                ]
                eligible = [
                    (action, context)
                    for action, context in zip(
                        generated_actions, generated_contexts, strict=True
                    )
                    if action.kind is not IndexActionKind.CREATE
                    or (
                        not context["exact_index_exists"]
                        and not context["prefix_index_exists"]
                    )
                ]
                actions = [action for action, _context in eligible]
                action_contexts = [context for _action, context in eligible]
                candidate_count = max(
                    candidate_count,
                    sum(
                        action.kind is IndexActionKind.CREATE for action in actions
                    ),
                )
                action_features = [
                    encode_action(
                        action,
                        sql_text,
                        encoding_version=SEQUENTIAL_ACTION_ENCODING,
                        database_context=context,
                    )
                    for action, context in zip(actions, action_contexts, strict=True)
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
                if not _should_measure_candidate(
                    values[best_index], threshold, len(accepted_steps)
                ):
                    terminal_reason = "model_stop"
                    break
                # A deep analysis is an explicit empirical verification. For a
                # slow query, verify the model's strongest eligible candidate
                # once even when its Q value is below the learned threshold.
                # The measured gain criteria below remain authoritative.
                previous_time = episode.state.current_time_ms
                try:
                    transition = episode.step(actions[best_index])
                except Exception as exc:
                    if not _is_statement_timeout(exc):
                        raise
                    terminal_reason = "statement_timeout"
                    break
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
                improvement_ratio = (
                    previous_time - transition.next_state.current_time_ms
                ) / max(previous_time, 0.001)
                if improvement_ratio < minimum_improvement_ratio:
                    terminal_reason = "relative_gain_too_small"
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
                        target_relation_rows=float(
                            action_contexts[best_index]["target_relation_rows"]
                        ),
                        target_relation_size_bytes=int(
                            action_contexts[best_index]["target_relation_size_bytes"]
                        ),
                        target_existing_index_count=int(
                            action_contexts[best_index]["target_index_count"]
                        ),
                        target_insert_count=int(
                            action_contexts[best_index]["target_insert_count"]
                        ),
                        target_update_count=int(
                            action_contexts[best_index]["target_update_count"]
                        ),
                        target_delete_count=int(
                            action_contexts[best_index]["target_delete_count"]
                        ),
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
        minimum_improvement_ratio=minimum_improvement_ratio,
        baseline_samples_ms=episode.state.baseline_samples_ms,
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


def _is_statement_timeout(exc: Exception) -> bool:
    """Recognize PostgreSQL query cancellation without hiding other errors."""
    return getattr(exc, "sqlstate", None) == "57014"
