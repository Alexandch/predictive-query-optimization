"""Safe collection of PostgreSQL execution plans for read-only queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import DatabaseSettings
from .plan_features import PlanFeatures, extract_plan_features


@dataclass(frozen=True, slots=True)
class ExplainResult:
    plan_json: Any
    features: PlanFeatures


def _assert_read_only_query(sql_text: str) -> str:
    normalized = sql_text.strip()
    if not normalized:
        raise ValueError("SQL query must not be empty")

    first_token = normalized.split(None, 1)[0].upper()
    if first_token not in {"SELECT", "WITH"}:
        raise ValueError("Only SELECT and WITH queries can be analyzed")

    normalized = normalized.rstrip().removesuffix(";")
    if ";" in normalized:
        raise ValueError("Only one SQL statement can be analyzed at a time")

    return normalized


def collect_explain(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    analyze: bool = True,
) -> ExplainResult:
    """Collect an estimated or actually executed plan in a read-only transaction."""
    query = _assert_read_only_query(sql_text)
    settings = settings or DatabaseSettings.from_env()

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on optional runtime setup
        raise RuntimeError("Install project dependencies before connecting to PostgreSQL") from exc

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            options = (
                "ANALYZE, BUFFERS, FORMAT JSON"
                if analyze
                else "FORMAT JSON"
            )
            row = connection.execute(
                f"EXPLAIN ({options}) {query}"
            ).fetchone()

    if row is None:
        raise RuntimeError("PostgreSQL returned no EXPLAIN result")

    plan_json = row[0]
    return ExplainResult(
        plan_json=plan_json,
        features=extract_plan_features(plan_json),
    )
