"""Transactional PostgreSQL environment for evaluating index actions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from statistics import median

from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .index_actions import IndexAction, IndexActionKind
from .plan_features import extract_plan_features


@dataclass(frozen=True, slots=True)
class IndexExperimentResult:
    action: IndexAction
    baseline_time_ms: float
    candidate_time_ms: float
    improvement_ratio: float
    reward: float
    candidate_plan_cost: float
    candidate_uses_index: bool


class IndexExperimentEnvironment:
    """Evaluate one action and always roll its database changes back."""

    def __init__(
        self,
        settings: DatabaseSettings | None = None,
        *,
        allowed_schemas: frozenset[str] = frozenset({"aviation"}),
        repetitions: int = 3,
        statement_timeout_ms: int = 10_000,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        self.settings = settings or DatabaseSettings.from_env()
        self.allowed_schemas = allowed_schemas
        self.repetitions = repetitions
        self.statement_timeout_ms = statement_timeout_ms

    def evaluate(self, sql_text: str, action: IndexAction) -> IndexExperimentResult:
        query = _assert_read_only_query(sql_text)
        if action.kind is IndexActionKind.CREATE:
            if action.schema_name not in self.allowed_schemas:
                raise ValueError("Index action targets a schema outside the allowlist")
            self._assert_action_targets_query(query, action)

        try:
            import psycopg
            from psycopg import sql
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install project dependencies before evaluation") from exc

        with psycopg.connect(**self.settings.connection_kwargs()) as connection:
            try:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("pqo-index-experiment",),
                )
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self.statement_timeout_ms}ms",),
                )
                before, _ = self._measure(connection, query)

                if action.kind is IndexActionKind.NOOP:
                    baseline = median(before)
                    return IndexExperimentResult(
                        action, baseline, baseline, 0.0, 0.0, 0.0, False
                    )

                connection.execute("SAVEPOINT pqo_index_trial")
                index_name = self._temporary_index_name(query, action)
                statement = sql.SQL("CREATE INDEX {} ON {}.{} ({})").format(
                    sql.Identifier(index_name),
                    sql.Identifier(action.schema_name),
                    sql.Identifier(action.table_name),
                    sql.SQL(", ").join(map(sql.Identifier, action.key_columns)),
                )
                if action.include_columns:
                    statement += sql.SQL(" INCLUDE ({})").format(
                        sql.SQL(", ").join(
                            map(sql.Identifier, action.include_columns)
                        )
                    )
                connection.execute(statement)
                candidate_times, candidate_plan = self._measure(connection, query)

                connection.execute("ROLLBACK TO SAVEPOINT pqo_index_trial")
                after, _ = self._measure(connection, query)

                baseline = median((*before, *after))
                candidate = median(candidate_times)
                improvement = (baseline - candidate) / max(baseline, 0.001)
                complexity_penalty = (
                    0.01 * len(action.key_columns)
                    + 0.005 * len(action.include_columns)
                )
                plan_text = str(candidate_plan)
                return IndexExperimentResult(
                    action=action,
                    baseline_time_ms=baseline,
                    candidate_time_ms=candidate,
                    improvement_ratio=improvement,
                    reward=improvement - complexity_penalty,
                    candidate_plan_cost=float(candidate_plan[0]["Plan"]["Total Cost"]),
                    candidate_uses_index="Index" in plan_text or "Bitmap" in plan_text,
                )
            finally:
                connection.rollback()

    def _measure(self, connection, query: str) -> tuple[list[float], object]:
        times: list[float] = []
        last_plan = None
        for _ in range(self.repetitions):
            row = connection.execute(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}"
            ).fetchone()
            if row is None:
                raise RuntimeError("PostgreSQL returned no EXPLAIN result")
            last_plan = row[0]
            features = extract_plan_features(last_plan)
            if features.actual_total_time_ms is None:
                raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
            times.append(features.actual_total_time_ms)
        return times, last_plan

    def _assert_action_targets_query(self, query: str, action: IndexAction) -> None:
        from sqlglot import exp, parse_one

        tables = {
            (table.db or "public", table.name)
            for table in parse_one(query, read="postgres").find_all(exp.Table)
        }
        if (action.schema_name, action.table_name) not in tables:
            raise ValueError("Index action targets a table absent from the query")

    @staticmethod
    def _temporary_index_name(query: str, action: IndexAction) -> str:
        digest = hashlib.sha256(f"{query}|{action}".encode()).hexdigest()[:16]
        return f"pqo_trial_{digest}"
