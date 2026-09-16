"""Persistence and validation of user feedback on structural recommendations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Sequence

from .config import DatabaseSettings
from .structural_advisor import StructuralRecommendation


class RecommendationDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MeasurementOutcome(StrEnum):
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"


@dataclass(frozen=True, slots=True)
class StructuralFeedbackUpdate:
    recommendation_id: int
    status: str
    measured_improvement_ratio: float | None = None
    measurement_outcome: MeasurementOutcome | None = None


def save_structural_recommendations(
    query_run_id: int,
    recommendations: Sequence[StructuralRecommendation],
    settings: DatabaseSettings | None = None,
    *,
    connection=None,
) -> tuple[StructuralRecommendation, ...]:
    """Persist recommendations and return copies carrying their database IDs."""
    import psycopg

    if query_run_id <= 0:
        raise ValueError("query_run_id must be positive")
    settings = settings or DatabaseSettings.from_env()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return save_structural_recommendations(
                query_run_id,
                recommendations,
                settings=settings,
                connection=owned_connection,
            )

    _require_feedback_schema(connection)
    saved = []
    with connection.transaction():
        for item in recommendations:
            recommendation_id = connection.execute(
                """
                INSERT INTO pqo.structural_recommendation (
                    query_run_id, category, rule_id, priority, title, evidence,
                    proposed_action, verification, suggested_sql
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (query_run_id, rule_id) DO UPDATE SET
                    category = EXCLUDED.category,
                    priority = EXCLUDED.priority,
                    title = EXCLUDED.title,
                    evidence = EXCLUDED.evidence,
                    proposed_action = EXCLUDED.proposed_action,
                    verification = EXCLUDED.verification,
                    suggested_sql = EXCLUDED.suggested_sql,
                    updated_at = clock_timestamp()
                RETURNING id
                """,
                (
                    query_run_id,
                    item.category.value,
                    item.rule_id,
                    item.priority.value,
                    item.title,
                    item.evidence,
                    item.action,
                    item.verification,
                    item.suggested_sql,
                ),
            ).fetchone()[0]
            saved.append(replace(item, recommendation_id=recommendation_id))
    return tuple(saved)


def record_recommendation_decision(
    recommendation_id: int,
    decision: RecommendationDecision | str,
    note: str = "",
    settings: DatabaseSettings | None = None,
    *,
    connection=None,
) -> StructuralFeedbackUpdate:
    """Record an explicit accept/reject decision without applying the action."""
    import psycopg

    decision = RecommendationDecision(decision)
    if recommendation_id <= 0:
        raise ValueError("recommendation_id must be positive")
    settings = settings or DatabaseSettings.from_env()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return record_recommendation_decision(
                recommendation_id,
                decision,
                note,
                settings=settings,
                connection=owned_connection,
            )

    _require_feedback_schema(connection)
    row = connection.execute(
        """
        UPDATE pqo.structural_recommendation
        SET status = %s,
            decision_note = NULLIF(%s, ''),
            decided_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = %s
        RETURNING id, status, measured_improvement_ratio, measurement_outcome
        """,
        (decision.value, note.strip(), recommendation_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Structural recommendation #{recommendation_id} not found")
    return _feedback_update(row)


def record_recommendation_measurement(
    recommendation_id: int,
    baseline_time_ms: float,
    optimized_time_ms: float,
    note: str = "",
    settings: DatabaseSettings | None = None,
    *,
    connection=None,
) -> StructuralFeedbackUpdate:
    """Store an independently measured outcome for an accepted recommendation."""
    import psycopg

    if recommendation_id <= 0:
        raise ValueError("recommendation_id must be positive")
    if baseline_time_ms <= 0:
        raise ValueError("baseline_time_ms must be positive")
    if optimized_time_ms < 0:
        raise ValueError("optimized_time_ms must be non-negative")
    ratio = (baseline_time_ms - optimized_time_ms) / baseline_time_ms
    tolerance = max(1.0 / baseline_time_ms, 0.02)
    if ratio > tolerance:
        outcome = MeasurementOutcome.IMPROVED
    elif ratio < -tolerance:
        outcome = MeasurementOutcome.REGRESSED
    else:
        outcome = MeasurementOutcome.UNCHANGED

    settings = settings or DatabaseSettings.from_env()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return record_recommendation_measurement(
                recommendation_id,
                baseline_time_ms,
                optimized_time_ms,
                note,
                settings=settings,
                connection=owned_connection,
            )

    _require_feedback_schema(connection)
    row = connection.execute(
        """
        UPDATE pqo.structural_recommendation
        SET baseline_time_ms = %s,
            optimized_time_ms = %s,
            measured_improvement_ratio = %s,
            measurement_outcome = %s,
            measurement_note = NULLIF(%s, ''),
            measured_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = %s AND status = 'accepted'
        RETURNING id, status, measured_improvement_ratio, measurement_outcome
        """,
        (
            baseline_time_ms,
            optimized_time_ms,
            ratio,
            outcome.value,
            note.strip(),
            recommendation_id,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(
            "Recommendation not found or must be accepted before measurement"
        )
    return _feedback_update(row)


def _require_feedback_schema(connection) -> None:
    ready = connection.execute(
        "SELECT to_regclass('pqo.structural_recommendation') IS NOT NULL"
    ).fetchone()[0]
    if not ready:
        raise RuntimeError(
            "pqo.structural_recommendation is missing; apply "
            "database/init/007_structural_feedback.sql"
        )


def _feedback_update(row) -> StructuralFeedbackUpdate:
    return StructuralFeedbackUpdate(
        recommendation_id=row[0],
        status=row[1],
        measured_improvement_ratio=row[2],
        measurement_outcome=(
            MeasurementOutcome(row[3]) if row[3] is not None else None
        ),
    )
