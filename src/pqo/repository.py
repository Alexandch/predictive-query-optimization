"""Persistence of collected queries and their feature vectors."""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from .config import DatabaseSettings
from .database_features import DatabaseFeatures
from .plan_features import PlanFeatures
from .sql_features import SQLFeatures


def save_analysis(
    sql_text: str,
    plan_json: Any,
    sql_features: SQLFeatures,
    plan_features: PlanFeatures,
    database_features: DatabaseFeatures,
    settings: DatabaseSettings | None = None,
    source: str = "dataset-collector",
    template_id: str = "external",
    connection=None,
) -> int:
    import psycopg

    settings = settings or DatabaseSettings.from_env()
    sql_hash = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()

    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return save_analysis(
                sql_text,
                plan_json,
                sql_features,
                plan_features,
                database_features,
                settings=settings,
                source=source,
                template_id=template_id,
                connection=owned_connection,
            )

    with connection.transaction():
        query_run_id = connection.execute(
            "SELECT pqo.register_query_run(%s, %s, %s, %s)",
            (sql_text, sql_hash, source, template_id),
        ).fetchone()[0]

        connection.execute(
            """
            INSERT INTO pqo.query_features (
                query_run_id,
                query_length,
                join_count,
                where_condition_count,
                subquery_count,
                has_group_by,
                has_order_by,
                has_distinct,
                table_reference_count,
                unique_table_count,
                select_expression_count,
                aggregate_function_count,
                inner_join_count,
                left_join_count,
                right_join_count,
                full_join_count,
                cross_join_count
            ) VALUES (
                %(query_run_id)s,
                %(query_length)s,
                %(join_count)s,
                %(where_condition_count)s,
                %(subquery_count)s,
                %(has_group_by)s,
                %(has_order_by)s,
                %(has_distinct)s,
                %(table_reference_count)s,
                %(unique_table_count)s,
                %(select_expression_count)s,
                %(aggregate_function_count)s,
                %(inner_join_count)s,
                %(left_join_count)s,
                %(right_join_count)s,
                %(full_join_count)s,
                %(cross_join_count)s
            )
            """,
            {"query_run_id": query_run_id, **sql_features.as_dict()},
        )

        connection.execute(
            """
            INSERT INTO pqo.execution_plan (
                query_run_id,
                plan_json,
                estimated_startup_cost,
                root_node_type,
                node_count,
                max_plan_depth,
                relation_count,
                estimated_total_cost,
                estimated_plan_rows,
                estimated_plan_width,
                estimated_rows_all_nodes,
                actual_total_time_ms,
                seq_scan_count,
                index_scan_count,
                index_only_scan_count,
                bitmap_heap_scan_count,
                hash_join_count,
                merge_join_count,
                nested_loop_count,
                sort_node_count,
                aggregate_node_count,
                shared_hit_blocks,
                shared_read_blocks,
                temp_read_blocks,
                temp_written_blocks,
                relation_row_estimate_sum,
                largest_relation_rows,
                relation_size_bytes,
                index_size_bytes,
                existing_index_count,
                estimated_selectivity
            ) VALUES (
                %(query_run_id)s,
                %(plan_json)s,
                %(estimated_startup_cost)s,
                %(root_node_type)s,
                %(node_count)s,
                %(max_plan_depth)s,
                %(relation_count)s,
                %(estimated_total_cost)s,
                %(estimated_plan_rows)s,
                %(estimated_plan_width)s,
                %(estimated_rows_all_nodes)s,
                %(actual_total_time_ms)s,
                %(seq_scan_count)s,
                %(index_scan_count)s,
                %(index_only_scan_count)s,
                %(bitmap_heap_scan_count)s,
                %(hash_join_count)s,
                %(merge_join_count)s,
                %(nested_loop_count)s,
                %(sort_node_count)s,
                %(aggregate_node_count)s,
                %(shared_hit_blocks)s,
                %(shared_read_blocks)s,
                %(temp_read_blocks)s,
                %(temp_written_blocks)s,
                %(relation_row_estimate_sum)s,
                %(largest_relation_rows)s,
                %(relation_size_bytes)s,
                %(index_size_bytes)s,
                %(existing_index_count)s,
                %(estimated_selectivity)s
            )
            """,
            {
                "query_run_id": query_run_id,
                "plan_json": Jsonb(plan_json),
                **plan_features.as_dict(),
                **database_features.as_dict(),
            },
        )

        connection.execute(
            """
            UPDATE pqo.query_run
            SET status = 'completed',
                finished_at = clock_timestamp(),
                execution_time_ms = %s
            WHERE id = %s
            """,
            (plan_features.actual_total_time_ms, query_run_id),
        )

    return query_run_id


def save_optimization_result(
    query_run_id: int,
    predicted_time_ms: float,
    recommendation,
    settings: DatabaseSettings | None = None,
    *,
    model_name: str = "xgboost",
    model_version: str = "52-templates-v2",
) -> None:
    """Persist model output produced for an already stored estimated plan."""
    import psycopg

    from .index_actions import IndexActionKind

    settings = settings or DatabaseSettings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute(
            """
            INSERT INTO pqo.model_prediction (
                query_run_id, model_name, model_version, predicted_time_ms
            ) VALUES (%s, %s, %s, %s)
            """,
            (query_run_id, model_name, model_version, predicted_time_ms),
        )
        if (
            recommendation is not None
            and recommendation.action.kind is IndexActionKind.CREATE
        ):
            action = recommendation.action
            connection.execute(
                """
                INSERT INTO pqo.index_recommendation (
                    query_run_id,
                    table_name,
                    index_columns,
                    include_columns,
                    action,
                    proposed_ddl,
                    estimated_improvement
                ) VALUES (%s, %s, %s, %s, 'create', %s, %s)
                """,
                (
                    query_run_id,
                    f"{action.schema_name}.{action.table_name}",
                    list(action.key_columns),
                    list(action.include_columns),
                    _proposed_ddl(action),
                    recommendation.predicted_reward,
                ),
            )


def save_sequential_analysis(
    sql_text: str,
    plan,
    settings: DatabaseSettings | None = None,
    *,
    query_run_id: int | None = None,
    model_version: str = "unknown",
    source: str = "application",
    connection=None,
) -> tuple[int, int]:
    """Persist a measured sequential plan and all of its accepted steps."""
    import psycopg

    settings = settings or DatabaseSettings.from_env()
    normalized_sql = sql_text.strip()
    sql_hash = hashlib.sha256(normalized_sql.encode("utf-8")).hexdigest()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return save_sequential_analysis(
                normalized_sql,
                plan,
                settings=settings,
                query_run_id=query_run_id,
                model_version=model_version,
                source=source,
                connection=owned_connection,
            )

    with connection.transaction():
        if query_run_id is None:
            query_run_id = connection.execute(
                """
                INSERT INTO pqo.query_run (
                    sql_text, sql_hash, source, status, finished_at,
                    execution_time_ms
                ) VALUES (%s, %s, %s, 'completed', clock_timestamp(), %s)
                RETURNING id
                """,
                (normalized_sql, sql_hash, source, plan.baseline_time_ms),
            ).fetchone()[0]
        else:
            existing = connection.execute(
                "SELECT sql_text FROM pqo.query_run WHERE id = %s",
                (query_run_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"Query run #{query_run_id} does not exist")
            if existing[0].strip() != normalized_sql:
                raise ValueError("Sequential analysis SQL does not match query run")
            connection.execute(
                """
                UPDATE pqo.query_run
                SET status = 'completed',
                    finished_at = COALESCE(finished_at, clock_timestamp()),
                    execution_time_ms = %s
                WHERE id = %s
                """,
                (plan.baseline_time_ms, query_run_id),
            )

        sequential_analysis_id = connection.execute(
            """
            INSERT INTO pqo.sequential_analysis (
                query_run_id, model_version, baseline_time_ms, final_time_ms,
                measured_improvement_ratio, storage_budget_bytes,
                used_budget_bytes, candidate_count, decision_threshold,
                minimum_baseline_time_ms, minimum_absolute_improvement_ms,
                terminal_reason
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) RETURNING id
            """,
            (
                query_run_id,
                model_version,
                plan.baseline_time_ms,
                plan.final_time_ms,
                plan.measured_improvement_ratio,
                plan.storage_budget_bytes,
                plan.used_budget_bytes,
                plan.candidate_count,
                plan.decision_threshold,
                plan.minimum_baseline_time_ms,
                plan.minimum_absolute_improvement_ms,
                plan.terminal_reason,
            ),
        ).fetchone()[0]

        for step_number, step in enumerate(plan.steps, start=1):
            action = step.action
            connection.execute(
                """
                INSERT INTO pqo.sequential_analysis_step (
                    sequential_analysis_id, step_number, schema_name,
                    table_name, key_columns, include_columns, proposed_ddl,
                    predicted_q, measured_reward, before_time_ms,
                    after_time_ms, index_size_bytes, creation_time_ms,
                    used_by_postgresql
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    sequential_analysis_id,
                    step_number,
                    action.schema_name,
                    action.table_name,
                    list(action.key_columns),
                    list(action.include_columns),
                    _proposed_ddl(action),
                    step.predicted_q,
                    step.measured_reward,
                    step.before_time_ms,
                    step.after_time_ms,
                    step.index_size_bytes,
                    step.creation_time_ms,
                    step.used_by_postgresql,
                ),
            )

    return query_run_id, sequential_analysis_id


def _proposed_ddl(action) -> str:
    keys = ", ".join(f'"{column}"' for column in action.key_columns)
    statement = (
        f'CREATE INDEX ON "{action.schema_name}"."{action.table_name}" ({keys})'
    )
    if action.include_columns:
        includes = ", ".join(f'"{column}"' for column in action.include_columns)
        statement += f" INCLUDE ({includes})"
    return statement + ";"
