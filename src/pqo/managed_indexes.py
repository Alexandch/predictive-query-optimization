"""Explicitly apply and safely roll back indexes approved by the user."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from statistics import median
from uuid import uuid4

from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .index_actions import IndexAction, IndexActionKind
from .plan_features import extract_plan_features


_MANAGED_INDEX_NAME = re.compile(r"^pqo_managed_[0-9a-f]{12}_[0-9a-f]{8}$")


@dataclass(frozen=True, slots=True)
class ManagedIndex:
    schema_name: str
    index_name: str
    table_name: str
    key_columns: tuple[str, ...]
    include_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedIndexDeployment:
    database_identity: str
    sql_text: str
    indexes: tuple[ManagedIndex, ...]
    baseline_time_ms: float
    indexed_time_ms: float

    @property
    def improvement_ratio(self) -> float:
        return (self.baseline_time_ms - self.indexed_time_ms) / max(
            self.baseline_time_ms, 0.001
        )

    @property
    def drop_statements(self) -> tuple[str, ...]:
        return tuple(
            f'DROP INDEX IF EXISTS "{item.schema_name}"."{item.index_name}";'
            for item in self.indexes
        )


@dataclass(frozen=True, slots=True)
class ManagedIndexRollback:
    dropped_indexes: tuple[ManagedIndex, ...]
    restored_time_ms: float


def _database_identity(settings: DatabaseSettings) -> str:
    return f"{settings.host.lower()}:{settings.port}/{settings.dbname}"


def _measure(connection, sql_text: str, repetitions: int) -> float:
    values = []
    for _ in range(repetitions):
        row = connection.execute(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql_text}"
        ).fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL returned no EXPLAIN result")
        value = extract_plan_features(row[0]).actual_total_time_ms
        if value is None:
            raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
        values.append(value)
    return median(values)


def _validate_actions(
    actions: tuple[IndexAction, ...], settings: DatabaseSettings
) -> None:
    if not 1 <= len(actions) <= 2:
        raise ValueError("One or two verified index actions are required")
    for action in actions:
        if action.kind is not IndexActionKind.CREATE:
            raise ValueError("Only CREATE INDEX actions can be applied")
        if action.schema_name not in settings.allowed_schemas:
            raise ValueError("Index schema is outside the configured allowlist")


def save_deployment(
    deployment: ManagedIndexDeployment, path: str | Path
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(deployment), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def load_deployment(path: str | Path) -> ManagedIndexDeployment:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    indexes = tuple(
        ManagedIndex(
            schema_name=str(item["schema_name"]),
            index_name=str(item["index_name"]),
            table_name=str(item["table_name"]),
            key_columns=tuple(item["key_columns"]),
            include_columns=tuple(item["include_columns"]),
        )
        for item in data["indexes"]
    )
    if not indexes or any(
        not _MANAGED_INDEX_NAME.fullmatch(item.index_name) for item in indexes
    ):
        raise ValueError("Managed-index record contains an unsafe index name")
    return ManagedIndexDeployment(
        database_identity=str(data["database_identity"]),
        sql_text=str(data["sql_text"]),
        indexes=indexes,
        baseline_time_ms=float(data["baseline_time_ms"]),
        indexed_time_ms=float(data["indexed_time_ms"]),
    )


def apply_verified_indexes(
    sql_text: str,
    actions: tuple[IndexAction, ...],
    deployment_path: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
) -> ManagedIndexDeployment:
    """Create approved indexes, commit them, and record how to remove them."""
    import hashlib
    import psycopg
    from psycopg import sql

    settings = settings or DatabaseSettings.from_env()
    query = _assert_read_only_query(sql_text)
    _validate_actions(actions, settings)
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    target = Path(deployment_path)
    if target.is_file():
        raise RuntimeError("Roll back the previously applied index plan first")

    suffix = uuid4().hex[:8]
    created: list[ManagedIndex] = []
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("pqo-managed-index-change",),
        )
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{settings.statement_timeout_ms}ms",),
        )
        _measure(connection, query, 1)  # warm the same connection before comparison
        baseline = _measure(connection, query, repetitions)
        for action in actions:
            digest = hashlib.sha256(repr(action).encode()).hexdigest()[:12]
            index_name = f"pqo_managed_{digest}_{suffix}"
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
            created.append(
                ManagedIndex(
                    schema_name=action.schema_name,
                    index_name=index_name,
                    table_name=action.table_name,
                    key_columns=action.key_columns,
                    include_columns=action.include_columns,
                )
            )
        indexed = _measure(connection, query, repetitions)
        connection.commit()

    deployment = ManagedIndexDeployment(
        database_identity=_database_identity(settings),
        sql_text=query,
        indexes=tuple(created),
        baseline_time_ms=baseline,
        indexed_time_ms=indexed,
    )
    try:
        save_deployment(deployment, target)
    except Exception:
        # Do not leave committed indexes behind if the rollback record was lost.
        _drop_indexes(deployment.indexes, settings)
        raise
    return deployment


def _drop_indexes(
    indexes: tuple[ManagedIndex, ...], settings: DatabaseSettings
) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("pqo-managed-index-change",),
        )
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{settings.statement_timeout_ms}ms",),
        )
        for item in indexes:
            if (
                item.schema_name not in settings.allowed_schemas
                or not _MANAGED_INDEX_NAME.fullmatch(item.index_name)
            ):
                raise ValueError("Refusing to drop an unmanaged index")
            connection.execute(
                sql.SQL("DROP INDEX IF EXISTS {}.{}").format(
                    sql.Identifier(item.schema_name),
                    sql.Identifier(item.index_name),
                )
            )
        connection.commit()


def rollback_verified_indexes(
    deployment_path: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
) -> ManagedIndexRollback:
    """Drop only indexes recorded as application-managed and remeasure the SQL."""
    import psycopg

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    settings = settings or DatabaseSettings.from_env()
    target = Path(deployment_path)
    deployment = load_deployment(target)
    if deployment.database_identity != _database_identity(settings):
        raise ValueError("Applied index plan belongs to another database")

    _drop_indexes(deployment.indexes, settings)
    query = _assert_read_only_query(deployment.sql_text)
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{settings.statement_timeout_ms}ms",),
        )
        restored = _measure(connection, query, repetitions)
    target.unlink(missing_ok=True)
    return ManagedIndexRollback(deployment.indexes, restored)
