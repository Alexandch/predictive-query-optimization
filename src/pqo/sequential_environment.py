"""Transactional multi-index environment with STOP and a storage budget."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from statistics import median
from time import perf_counter

from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .index_actions import (
    IndexAction,
    IndexActionKind,
    generate_sequential_index_actions,
)
from .plan_features import extract_plan_features


@dataclass(frozen=True, slots=True)
class SequentialIndexState:
    step: int
    max_steps: int
    baseline_time_ms: float
    current_time_ms: float
    storage_budget_bytes: int
    used_budget_bytes: int
    selected_actions: tuple[IndexAction, ...]
    selected_index_bytes: tuple[int, ...]
    cumulative_improvement_ratio: float
    done: bool

    @property
    def remaining_budget_bytes(self) -> int:
        return max(0, self.storage_budget_bytes - self.used_budget_bytes)


@dataclass(frozen=True, slots=True)
class SequentialIndexTransition:
    previous_state: SequentialIndexState
    action: IndexAction
    next_state: SequentialIndexState
    reward: float
    incremental_improvement_ratio: float
    index_size_bytes: int
    creation_time_ms: float
    candidate_uses_created_index: bool
    candidate_uses_selected_index: bool
    accepted: bool
    terminal_reason: str | None


class SequentialIndexEnvironment:
    """Create up to ``max_steps`` indexes and roll the entire episode back."""

    def __init__(
        self,
        settings: DatabaseSettings | None = None,
        *,
        allowed_schemas: frozenset[str] = frozenset({"aviation"}),
        repetitions: int = 2,
        statement_timeout_ms: int = 10_000,
        max_steps: int = 3,
        storage_budget_bytes: int = 64 * 1024 * 1024,
        storage_penalty_weight: float = 0.05,
        creation_penalty_weight: float = 0.01,
        budget_violation_penalty: float = 0.10,
        warmup_runs: int = 1,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if storage_budget_bytes < 1:
            raise ValueError("storage_budget_bytes must be positive")
        if warmup_runs < 0:
            raise ValueError("warmup_runs must be non-negative")
        if min(
            storage_penalty_weight,
            creation_penalty_weight,
            budget_violation_penalty,
        ) < 0:
            raise ValueError("reward penalties must be non-negative")
        self.settings = settings or DatabaseSettings.from_env()
        self.allowed_schemas = allowed_schemas
        self.repetitions = repetitions
        self.statement_timeout_ms = statement_timeout_ms
        self.max_steps = max_steps
        self.storage_budget_bytes = storage_budget_bytes
        self.storage_penalty_weight = storage_penalty_weight
        self.creation_penalty_weight = creation_penalty_weight
        self.budget_violation_penalty = budget_violation_penalty
        self.warmup_runs = warmup_runs

    @contextmanager
    def episode(self, sql_text: str, connection=None):
        """Yield a live episode and always remove every trial index on exit."""
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install project dependencies before evaluation") from exc

        if connection is None:
            with psycopg.connect(**self.settings.connection_kwargs()) as owned:
                try:
                    yield self._start_episode(sql_text, owned)
                finally:
                    owned.rollback()
            return

        try:
            yield self._start_episode(sql_text, connection)
        finally:
            connection.rollback()

    def _start_episode(self, sql_text: str, connection):
        query = _assert_read_only_query(sql_text)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("pqo-sequential-index-experiment",),
        )
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{self.statement_timeout_ms}ms",),
        )
        for _ in range(self.warmup_runs):
            self._measure(connection, query)
        times, _ = self._measure(connection, query)
        baseline = median(times)
        candidates = generate_sequential_index_actions(
            query,
            allowed_schemas=self.allowed_schemas,
        )
        return SequentialIndexEpisode(
            environment=self,
            connection=connection,
            query=query,
            candidates=candidates,
            state=SequentialIndexState(
                step=0,
                max_steps=self.max_steps,
                baseline_time_ms=baseline,
                current_time_ms=baseline,
                storage_budget_bytes=self.storage_budget_bytes,
                used_budget_bytes=0,
                selected_actions=(),
                selected_index_bytes=(),
                cumulative_improvement_ratio=0.0,
                done=False,
            ),
        )

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


class SequentialIndexEpisode:
    """Mutable transaction-scoped episode owned by the environment context."""

    def __init__(self, *, environment, connection, query, candidates, state) -> None:
        self.environment = environment
        self.connection = connection
        self.query = query
        self._candidates = candidates
        self.state = state
        self._created_index_names: list[str] = []

    @property
    def available_actions(self) -> tuple[IndexAction, ...]:
        if self.state.done:
            return ()
        selected = set(self.state.selected_actions)
        return tuple(
            action
            for action in self._candidates
            if action.kind is IndexActionKind.STOP or action not in selected
        )

    def step(self, action: IndexAction) -> SequentialIndexTransition:
        if self.state.done:
            raise RuntimeError("Episode is already complete")
        if action not in self.available_actions:
            raise ValueError("Action is not available in the current episode")
        previous = self.state
        if action.kind is IndexActionKind.STOP:
            self.state = self._next_state(done=True)
            return SequentialIndexTransition(
                previous_state=previous,
                action=action,
                next_state=self.state,
                reward=0.0,
                incremental_improvement_ratio=0.0,
                index_size_bytes=0,
                creation_time_ms=0.0,
                candidate_uses_created_index=False,
                candidate_uses_selected_index=False,
                accepted=True,
                terminal_reason="stop",
            )
        if action.kind is not IndexActionKind.CREATE:
            raise ValueError("Sequential episodes support only CREATE and STOP")

        from psycopg import sql

        savepoint = f"pqo_seq_step_{previous.step + 1}"
        self.connection.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(savepoint)))
        index_name = self._index_name(action, previous.step + 1)
        statement = sql.SQL("CREATE INDEX {} ON {}.{} ({})").format(
            sql.Identifier(index_name),
            sql.Identifier(action.schema_name),
            sql.Identifier(action.table_name),
            sql.SQL(", ").join(map(sql.Identifier, action.key_columns)),
        )
        if action.include_columns:
            statement += sql.SQL(" INCLUDE ({})").format(
                sql.SQL(", ").join(map(sql.Identifier, action.include_columns))
            )
        started = perf_counter()
        self.connection.execute(statement)
        creation_time_ms = (perf_counter() - started) * 1000.0
        index_tree = self.connection.execute(
            """
            SELECT c.relname, pg_relation_size(c.oid)
            FROM pg_partition_tree(%s::regclass) AS tree
            JOIN pg_class AS c ON c.oid = tree.relid
            """,
            (f"{action.schema_name}.{index_name}",),
        ).fetchall()
        if not index_tree:
            index_tree = self.connection.execute(
                "SELECT relname, pg_relation_size(oid) "
                "FROM pg_class WHERE oid = %s::regclass",
                (f"{action.schema_name}.{index_name}",),
            ).fetchall()
        index_size = sum(int(row[1]) for row in index_tree)
        if index_size > previous.remaining_budget_bytes:
            self.connection.execute(
                sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(savepoint))
            )
            self.connection.execute(
                sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(savepoint))
            )
            return SequentialIndexTransition(
                previous_state=previous,
                action=action,
                next_state=previous,
                reward=-self.environment.budget_violation_penalty,
                incremental_improvement_ratio=0.0,
                index_size_bytes=index_size,
                creation_time_ms=creation_time_ms,
                candidate_uses_created_index=False,
                candidate_uses_selected_index=False,
                accepted=False,
                terminal_reason="budget_exceeded",
            )

        candidate_times, plan = self.environment._measure(self.connection, self.query)
        self.connection.execute(
            sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(savepoint))
        )
        measured_time = median(candidate_times)
        plan_index_names = _plan_index_names(plan)
        created_index_names = {str(row[0]) for row in index_tree}
        uses_created_index = bool(plan_index_names.intersection(created_index_names))
        current_time = measured_time if uses_created_index else previous.current_time_ms
        incremental = (
            previous.current_time_ms - current_time
        ) / max(previous.baseline_time_ms, 0.001)
        storage_penalty = self.environment.storage_penalty_weight * (
            index_size / self.environment.storage_budget_bytes
        )
        creation_penalty = self.environment.creation_penalty_weight * min(
            creation_time_ms / 1000.0,
            1.0,
        )
        complexity_penalty = (
            0.01 * len(action.key_columns)
            + 0.005 * len(action.include_columns)
        )
        reward = incremental - storage_penalty - creation_penalty - complexity_penalty
        self._created_index_names.extend(created_index_names)
        next_step = previous.step + 1
        used_budget = previous.used_budget_bytes + index_size
        done = next_step >= previous.max_steps or used_budget >= previous.storage_budget_bytes
        reason = "max_steps" if next_step >= previous.max_steps else None
        if used_budget >= previous.storage_budget_bytes:
            reason = "budget_exhausted"
        cumulative = (
            previous.baseline_time_ms - current_time
        ) / max(previous.baseline_time_ms, 0.001)
        self.state = SequentialIndexState(
            step=next_step,
            max_steps=previous.max_steps,
            baseline_time_ms=previous.baseline_time_ms,
            current_time_ms=current_time,
            storage_budget_bytes=previous.storage_budget_bytes,
            used_budget_bytes=used_budget,
            selected_actions=(*previous.selected_actions, action),
            selected_index_bytes=(*previous.selected_index_bytes, index_size),
            cumulative_improvement_ratio=cumulative,
            done=done,
        )
        return SequentialIndexTransition(
            previous_state=previous,
            action=action,
            next_state=self.state,
            reward=reward,
            incremental_improvement_ratio=incremental,
            index_size_bytes=index_size,
            creation_time_ms=creation_time_ms,
            candidate_uses_created_index=uses_created_index,
            candidate_uses_selected_index=bool(
                plan_index_names.intersection(self._created_index_names)
            ),
            accepted=True,
            terminal_reason=reason,
        )

    def _next_state(self, *, done: bool) -> SequentialIndexState:
        state = self.state
        return SequentialIndexState(
            step=state.step,
            max_steps=state.max_steps,
            baseline_time_ms=state.baseline_time_ms,
            current_time_ms=state.current_time_ms,
            storage_budget_bytes=state.storage_budget_bytes,
            used_budget_bytes=state.used_budget_bytes,
            selected_actions=state.selected_actions,
            selected_index_bytes=state.selected_index_bytes,
            cumulative_improvement_ratio=state.cumulative_improvement_ratio,
            done=done,
        )

    def _index_name(self, action: IndexAction, step: int) -> str:
        digest = hashlib.sha256(f"{self.query}|{step}|{action}".encode()).hexdigest()[:12]
        return f"pqo_seq_{step}_{digest}"


def _plan_index_names(value) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "Index Name" and isinstance(child, str):
                names.add(child)
            else:
                names.update(_plan_index_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_plan_index_names(child))
    return names
