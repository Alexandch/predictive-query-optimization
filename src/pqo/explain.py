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
    from sqlglot import exp, parse
    from sqlglot.errors import ParseError

    normalized = sql_text.strip()
    if not normalized:
        raise ValueError("SQL query must not be empty")

    first_token = normalized.split(None, 1)[0].upper()
    if first_token not in {"SELECT", "WITH"}:
        raise ValueError("Only SELECT and WITH queries can be analyzed")

    normalized = normalized.rstrip().removesuffix(";")
    if ";" in normalized:
        raise ValueError("Only one SQL statement can be analyzed at a time")

    try:
        statements = [item for item in parse(normalized, read="postgres") if item]
    except ParseError as exc:
        raise ValueError(f"Invalid PostgreSQL SQL: {exc}") from exc
    if len(statements) != 1:
        raise ValueError("Only one SQL statement can be analyzed at a time")

    statement = statements[0]
    forbidden_types = tuple(
        expression_type
        for name in (
            "Alter",
            "Command",
            "Copy",
            "Create",
            "Delete",
            "Drop",
            "Insert",
            "Merge",
            "Transaction",
            "Update",
        )
        if (expression_type := getattr(exp, name, None)) is not None
    )
    if any(isinstance(node, forbidden_types) for node in statement.walk()):
        raise ValueError("Data-changing SQL is not allowed in analyzed queries")

    return normalized


def collect_explain(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    analyze: bool = True,
    connection=None,
) -> ExplainResult:
    """Collect an estimated or actually executed plan in a read-only transaction."""
    query = _assert_read_only_query(sql_text)
    settings = settings or DatabaseSettings.from_env()

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on optional runtime setup
        raise RuntimeError("Install project dependencies before connecting to PostgreSQL") from exc

    owns_connection = connection is None
    connection = connection or psycopg.connect(**settings.connection_kwargs())
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{settings.statement_timeout_ms}ms",),
            )
            options = (
                "ANALYZE, BUFFERS, FORMAT JSON"
                if analyze
                else "FORMAT JSON"
            )
            row = connection.execute(
                f"EXPLAIN ({options}) {query}"
            ).fetchone()
    finally:
        if owns_connection:
            connection.close()

    if row is None:
        raise RuntimeError("PostgreSQL returned no EXPLAIN result")

    plan_json = row[0]
    return ExplainResult(
        plan_json=plan_json,
        features=extract_plan_features(plan_json),
    )
