"""Read-only access to analyses displayed by the desktop application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import DatabaseSettings


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
        rows = connection.execute(
            """
            SELECT
                qr.id,
                qr.started_at,
                qr.status,
                qr.sql_text,
                prediction.predicted_time_ms,
                ep.root_node_type,
                recommendation.table_name,
                recommendation.index_columns,
                recommendation.estimated_improvement
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
            WHERE qr.source = 'application'
            ORDER BY qr.started_at DESC
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
