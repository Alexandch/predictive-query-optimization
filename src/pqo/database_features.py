"""PostgreSQL catalog features for relations referenced by a query."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from .config import DatabaseSettings


@dataclass(frozen=True, slots=True)
class DatabaseFeatures:
    relation_row_estimate_sum: float
    largest_relation_rows: float
    relation_size_bytes: int
    index_size_bytes: int
    existing_index_count: int
    estimated_selectivity: float

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


def extract_relation_references(sql_text: str) -> tuple[tuple[str, str], ...]:
    """Return distinct physical relation references, excluding CTE aliases."""
    try:
        tree = parse_one(sql_text.strip(), read="postgres")
    except ParseError as exc:
        raise ValueError(f"Invalid PostgreSQL SQL: {exc}") from exc
    if tree is None:
        raise ValueError("SQL query must not be empty")

    cte_names = {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    references: set[tuple[str, str]] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name or name in cte_names:
            continue
        schema = (table.db or "public").lower()
        references.add((schema, name))
    return tuple(sorted(references))


def collect_database_features(
    sql_text: str,
    estimated_plan_rows: float,
    settings: DatabaseSettings | None = None,
    connection=None,
) -> DatabaseFeatures:
    """Read stable optimizer statistics without executing the analyzed query."""
    import psycopg

    references = extract_relation_references(sql_text)
    if not references:
        return DatabaseFeatures(0.0, 0.0, 0, 0, 0, 0.0)

    settings = settings or DatabaseSettings.from_env()
    schemas = sorted({schema for schema, _ in references})
    owns_connection = connection is None
    connection = connection or psycopg.connect(**settings.connection_kwargs())
    try:
        with connection.transaction():
            rows = connection.execute(
                """
                SELECT
                    n.nspname,
                    c.relname,
                    greatest(c.reltuples, 0)::double precision,
                    pg_total_relation_size(c.oid),
                    pg_indexes_size(c.oid),
                    (SELECT count(*) FROM pg_index AS i WHERE i.indrelid = c.oid)
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname = ANY(%s)
                """,
                (schemas,),
            ).fetchall()
    finally:
        if owns_connection:
            connection.close()

    wanted = set(references)
    matched = [
        row for row in rows if (row[0].lower(), row[1].lower()) in wanted
    ]
    row_counts = [float(row[2]) for row in matched]
    row_sum = sum(row_counts)
    largest_rows = max(row_counts, default=0.0)
    selectivity = (
        min(1.0, max(0.0, float(estimated_plan_rows)) / largest_rows)
        if largest_rows > 0
        else 0.0
    )
    return DatabaseFeatures(
        relation_row_estimate_sum=row_sum,
        largest_relation_rows=largest_rows,
        relation_size_bytes=sum(int(row[3]) for row in matched),
        index_size_bytes=sum(int(row[4]) for row in matched),
        existing_index_count=sum(int(row[5]) for row in matched),
        estimated_selectivity=selectivity,
    )
