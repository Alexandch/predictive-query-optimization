"""Rebuild ML-ready CSV files from execution plans persisted in PostgreSQL."""

from __future__ import annotations

import csv
from pathlib import Path

from .config import DatabaseSettings
from .plan_features import extract_plan_features
from .sql_features import extract_sql_features


def export_recent_dataset(
    output_path: str | Path,
    limit: int,
    settings: DatabaseSettings | None = None,
) -> int:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    import psycopg

    settings = settings or DatabaseSettings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        rows = connection.execute(
            """
            SELECT
                qr.template_id,
                qr.sql_text,
                ep.plan_json,
                ep.relation_row_estimate_sum,
                ep.largest_relation_rows,
                ep.relation_size_bytes,
                ep.index_size_bytes,
                ep.existing_index_count,
                ep.estimated_selectivity
            FROM pqo.query_run AS qr
            JOIN pqo.execution_plan AS ep ON ep.query_run_id = qr.id
            WHERE qr.status = 'completed'
              AND ep.actual_total_time_ms IS NOT NULL
            ORDER BY qr.id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    if not rows:
        raise ValueError("No completed query measurements were found")

    records = []
    for (
        template_id,
        sql_text,
        plan_json,
        relation_row_estimate_sum,
        largest_relation_rows,
        relation_size_bytes,
        index_size_bytes,
        existing_index_count,
        estimated_selectivity,
    ) in reversed(rows):
        records.append(
            {
                "template_id": template_id,
                "sql_text": sql_text,
                **extract_sql_features(sql_text).as_dict(),
                **extract_plan_features(plan_json).as_dict(),
                "relation_row_estimate_sum": relation_row_estimate_sum,
                "largest_relation_rows": largest_relation_rows,
                "relation_size_bytes": relation_size_bytes,
                "index_size_bytes": index_size_bytes,
                "existing_index_count": existing_index_count,
                "estimated_selectivity": estimated_selectivity,
            }
        )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    return len(records)

