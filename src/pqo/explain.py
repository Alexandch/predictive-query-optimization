"""Safe collection of PostgreSQL execution plans for read-only queries."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

from .config import DatabaseSettings
from .plan_features import PlanFeatures, extract_plan_features


@dataclass(frozen=True, slots=True)
class ExplainResult:
    plan_json: Any
    features: PlanFeatures


@dataclass(frozen=True, slots=True)
class ExecutionMeasurement:
    """A warm-cache execution measurement based on several real runs."""

    samples_ms: tuple[float, ...]
    warmup_runs: int
    last_result: ExplainResult

    @property
    def median_time_ms(self) -> float:
        return float(median(self.samples_ms))

    @property
    def minimum_time_ms(self) -> float:
        return min(self.samples_ms)

    @property
    def maximum_time_ms(self) -> float:
        return max(self.samples_ms)


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


def measure_query_execution(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    *,
    warmup_runs: int = 1,
    repetitions: int = 5,
    connection=None,
) -> ExecutionMeasurement:
    """Warm the query once, then return a robust median and observed range."""
    if warmup_runs < 0:
        raise ValueError("warmup_runs must be non-negative")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    settings = settings or DatabaseSettings.from_env()

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("Install project dependencies before connecting to PostgreSQL") from exc

    owns_connection = connection is None
    connection = connection or psycopg.connect(**settings.connection_kwargs())
    try:
        for _ in range(warmup_runs):
            collect_explain(sql_text, settings=settings, analyze=True, connection=connection)
        results = tuple(
            collect_explain(sql_text, settings=settings, analyze=True, connection=connection)
            for _ in range(repetitions)
        )
    finally:
        if owns_connection:
            connection.close()

    samples = tuple(
        result.features.actual_total_time_ms for result in results
    )
    if any(value is None for value in samples):
        raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
    return ExecutionMeasurement(
        samples_ms=tuple(float(value) for value in samples),
        warmup_runs=warmup_runs,
        last_result=results[-1],
    )
