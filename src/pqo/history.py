"""Read-only access to analyses displayed by the desktop application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import DatabaseSettings


@dataclass(frozen=True, slots=True)
class HistorySequentialStep:
    step_number: int
    proposed_ddl: str
    predicted_q: float
    measured_reward: float
    before_time_ms: float
    after_time_ms: float
    index_size_bytes: int
    used_by_postgresql: bool


@dataclass(frozen=True, slots=True)
class HistoryStructuralFeedback:
    recommendation_id: int
    category: str
    rule_id: str
    title: str
    status: str
    measured_improvement_ratio: float | None
    measurement_outcome: str | None


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    query_run_id: int
    started_at: datetime
    status: str
    sql_text: str
    predicted_time_ms: float | None
    root_node_type: str | None
    recommended_table: str | None
    recommended_columns: tuple[str, ...]
    predicted_reward: float | None
    sequential_analysis_id: int | None = None
    measured_baseline_time_ms: float | None = None
    measured_final_time_ms: float | None = None
    measured_improvement_ratio: float | None = None
    sequential_terminal_reason: str | None = None
    sequential_steps: tuple[HistorySequentialStep, ...] = ()
    structural_feedback: tuple[HistoryStructuralFeedback, ...] = ()


def load_analysis_history(
    settings: DatabaseSettings | None = None,
    *,
    limit: int = 200,
) -> list[HistoryRecord]:
    if limit <= 0 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")

    import psycopg

    settings = settings or DatabaseSettings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        sequential_history_ready, structural_feedback_ready = connection.execute(
            "SELECT "
            "to_regclass('pqo.sequential_analysis') IS NOT NULL "
            "AND to_regclass('pqo.sequential_analysis_step') IS NOT NULL, "
            "to_regclass('pqo.structural_recommendation') IS NOT NULL"
        ).fetchone()
        sequential_columns = (
            """
                sequential.id,
                sequential.baseline_time_ms,
                sequential.final_time_ms,
                sequential.measured_improvement_ratio,
                sequential.terminal_reason,
                sequential_steps.steps
            """
            if sequential_history_ready
            else "NULL, NULL, NULL, NULL, NULL, NULL"
        )
        sequential_join = (
            """
            LEFT JOIN pqo.sequential_analysis AS sequential
                ON sequential.query_run_id = qr.id
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'step_number', step.step_number,
                        'proposed_ddl', step.proposed_ddl,
                        'predicted_q', step.predicted_q,
                        'measured_reward', step.measured_reward,
                        'before_time_ms', step.before_time_ms,
                        'after_time_ms', step.after_time_ms,
                        'index_size_bytes', step.index_size_bytes,
                        'used_by_postgresql', step.used_by_postgresql
                    ) ORDER BY step.step_number
                ) AS steps
                FROM pqo.sequential_analysis_step AS step
                WHERE step.sequential_analysis_id = sequential.id
            ) AS sequential_steps ON true
            """
            if sequential_history_ready
            else ""
        )
        structural_feedback_column = (
            "structural_feedback.items" if structural_feedback_ready else "NULL"
        )
        structural_feedback_join = (
            """
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'recommendation_id', feedback.id,
                        'category', feedback.category,
                        'rule_id', feedback.rule_id,
                        'title', feedback.title,
                        'status', feedback.status,
                        'measured_improvement_ratio',
                            feedback.measured_improvement_ratio,
                        'measurement_outcome', feedback.measurement_outcome
                    ) ORDER BY feedback.id
                ) AS items
                FROM pqo.structural_recommendation AS feedback
                WHERE feedback.query_run_id = qr.id
            ) AS structural_feedback ON true
            """
            if structural_feedback_ready
            else ""
        )
        event_time = (
            "COALESCE(sequential.created_at, qr.started_at)"
            if sequential_history_ready
            else "qr.started_at"
        )
        rows = connection.execute(
            f"""
            SELECT
                qr.id,
                {event_time},
                qr.status,
                qr.sql_text,
                prediction.predicted_time_ms,
                ep.root_node_type,
                recommendation.table_name,
                recommendation.index_columns,
                recommendation.estimated_improvement,
                {sequential_columns},
                {structural_feedback_column}
            FROM pqo.query_run AS qr
            LEFT JOIN pqo.execution_plan AS ep ON ep.query_run_id = qr.id
            LEFT JOIN LATERAL (
                SELECT mp.predicted_time_ms
                FROM pqo.model_prediction AS mp
                WHERE mp.query_run_id = qr.id
                ORDER BY mp.created_at DESC
                LIMIT 1
            ) AS prediction ON true
            LEFT JOIN LATERAL (
                SELECT ir.table_name, ir.index_columns, ir.estimated_improvement
                FROM pqo.index_recommendation AS ir
                WHERE ir.query_run_id = qr.id
                ORDER BY ir.created_at DESC
                LIMIT 1
            ) AS recommendation ON true
            {sequential_join}
            {structural_feedback_join}
            WHERE qr.source = 'application'
            ORDER BY {event_time} DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    return [
        HistoryRecord(
            query_run_id=row[0],
            started_at=row[1],
            status=row[2],
            sql_text=row[3],
            predicted_time_ms=row[4],
            root_node_type=row[5],
            recommended_table=row[6],
            recommended_columns=tuple(row[7] or ()),
            predicted_reward=row[8],
            sequential_analysis_id=row[9],
            measured_baseline_time_ms=row[10],
            measured_final_time_ms=row[11],
            measured_improvement_ratio=row[12],
            sequential_terminal_reason=row[13],
            sequential_steps=tuple(
                HistorySequentialStep(
                    step_number=step["step_number"],
                    proposed_ddl=step["proposed_ddl"],
                    predicted_q=step["predicted_q"],
                    measured_reward=step["measured_reward"],
                    before_time_ms=step["before_time_ms"],
                    after_time_ms=step["after_time_ms"],
                    index_size_bytes=step["index_size_bytes"],
                    used_by_postgresql=step["used_by_postgresql"],
                )
                for step in (row[14] or ())
            ),
            structural_feedback=tuple(
                HistoryStructuralFeedback(
                    recommendation_id=item["recommendation_id"],
                    category=item["category"],
                    rule_id=item["rule_id"],
                    title=item["title"],
                    status=item["status"],
                    measured_improvement_ratio=item[
                        "measured_improvement_ratio"
                    ],
                    measurement_outcome=item["measurement_outcome"],
                )
                for item in (row[15] or ())
            ),
        )
        for row in rows
    ]


def check_database_connection(settings: DatabaseSettings) -> str:
    import psycopg

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        version, database, history_ready = connection.execute(
            "SELECT version(), current_database(), "
            "to_regclass('pqo.query_run') IS NOT NULL"
        ).fetchone()
    history_status = (
        "история pqo доступна"
        if history_ready
        else "анализ доступен, история pqo не установлена"
    )
    return f"{database} · {version.split(',')[0]} · {history_status}"
