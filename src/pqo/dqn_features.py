"""Stable numeric representations of query states and index actions."""

from __future__ import annotations

import hashlib
import math

from .config import DatabaseSettings
from .database_features import collect_database_features
from .explain import collect_explain
from .index_actions import IndexAction, IndexActionKind
from .sql_features import extract_sql_features
from .training import NUMERIC_FEATURES


ROOT_NODE_TYPES = (
    "Aggregate",
    "GroupAggregate",
    "Hash Join",
    "Index Only Scan",
    "Index Scan",
    "Limit",
    "Merge Join",
    "Nested Loop",
    "Seq Scan",
    "Sort",
    "Unique",
    "Other",
)
AVIATION_TABLES = (
    "airports",
    "aircrafts",
    "seats",
    "flights",
    "bookings",
    "tickets",
    "ticket_flights",
    "boarding_passes",
    "other",
)
LEGACY_ACTION_ENCODING = "aviation-v1"
GENERIC_ACTION_ENCODING = "generic-v2"
GENERIC_V3_ACTION_ENCODING = "generic-v3"
DEFAULT_ACTION_ENCODING = GENERIC_ACTION_ENCODING
TABLE_HASH_BUCKETS = len(AVIATION_TABLES) - 1
COLUMN_HASH_BUCKETS = 24

STATE_FEATURE_NAMES = tuple(NUMERIC_FEATURES) + tuple(
    f"root_node_{name}" for name in ROOT_NODE_TYPES
)
_ACTION_PREFIX_NAMES = (
    "is_noop",
    "is_create",
    "key_count",
    "include_count",
    "is_composite",
    "is_covering",
)
_ACTION_SUFFIX_NAMES = (
    *(f"key_column_bucket_{index}" for index in range(COLUMN_HASH_BUCKETS)),
    *(f"include_column_bucket_{index}" for index in range(COLUMN_HASH_BUCKETS)),
    "key_in_where_count",
    "key_in_join_count",
    "key_in_order_count",
    "include_in_select_count",
    "leading_key_in_where",
    "leading_key_in_join",
    "leading_key_in_order",
    "target_table_reference_count",
)
LEGACY_ACTION_FEATURE_NAMES = (
    *_ACTION_PREFIX_NAMES,
    *(f"table_{name}" for name in AVIATION_TABLES),
    *_ACTION_SUFFIX_NAMES,
)
ACTION_FEATURE_NAMES = (
    *_ACTION_PREFIX_NAMES,
    *(f"table_name_bucket_{index}" for index in range(TABLE_HASH_BUCKETS)),
    "is_public_schema",
    *_ACTION_SUFFIX_NAMES,
)
ACTION_V3_FEATURE_NAMES = (
    *ACTION_FEATURE_NAMES,
    "target_relation_rows_log",
    "target_relation_size_bytes_log",
    "target_index_count_log",
    "exact_index_exists",
    "prefix_index_exists",
    "non_sargable_key_count",
    "leading_key_non_sargable",
)


def build_query_state(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    connection=None,
) -> list[float]:
    sql_features = extract_sql_features(sql_text).as_dict()
    plan_features = collect_explain(
        sql_text, settings=settings, analyze=False, connection=connection
    ).features.as_dict()
    database_features = collect_database_features(
        sql_text,
        plan_features["estimated_plan_rows"],
        settings=settings,
        connection=connection,
    ).as_dict()
    return encode_query_state({**sql_features, **plan_features, **database_features})


def encode_query_state(values: dict) -> list[float]:
    """Encode already extracted features without another database request."""

    numeric = [
        math.log1p(max(0.0, float(values[name] or 0.0)))
        for name in NUMERIC_FEATURES
    ]
    root_type = str(values["root_node_type"])
    if root_type not in ROOT_NODE_TYPES:
        root_type = "Other"
    root_encoding = [float(root_type == name) for name in ROOT_NODE_TYPES]
    return [*numeric, *root_encoding]


def encode_action(
    action: IndexAction,
    sql_text: str | None = None,
    *,
    encoding_version: str = DEFAULT_ACTION_ENCODING,
    database_context: dict[str, int | float | bool] | None = None,
) -> list[float]:
    prefix = [
        float(action.kind is IndexActionKind.NOOP),
        float(action.kind is IndexActionKind.CREATE),
        float(len(action.key_columns)),
        float(len(action.include_columns)),
        float(len(action.key_columns) > 1),
        float(bool(action.include_columns)),
    ]
    if encoding_version == LEGACY_ACTION_ENCODING:
        table_name = action.table_name or "other"
        if table_name not in AVIATION_TABLES:
            table_name = "other"
        tables = [float(table_name == name) for name in AVIATION_TABLES]
    elif encoding_version in {GENERIC_ACTION_ENCODING, GENERIC_V3_ACTION_ENCODING}:
        tables = _hashed_table(action.table_name)
        tables.append(float(action.schema_name == "public"))
    else:
        raise ValueError(f"Unsupported action encoding: {encoding_version}")
    keys = _hashed_columns(action.key_columns)
    includes = _hashed_columns(action.include_columns)
    context = _action_query_context(action, sql_text)
    result = [*prefix, *tables, *keys, *includes, *context]
    if encoding_version == GENERIC_V3_ACTION_ENCODING:
        result.extend(
            _optimizer_action_context(
                action,
                sql_text,
                database_context or {},
            )
        )
    return result


def action_feature_names(encoding_version: str) -> tuple[str, ...]:
    if encoding_version == LEGACY_ACTION_ENCODING:
        return LEGACY_ACTION_FEATURE_NAMES
    if encoding_version == GENERIC_ACTION_ENCODING:
        return ACTION_FEATURE_NAMES
    if encoding_version == GENERIC_V3_ACTION_ENCODING:
        return ACTION_V3_FEATURE_NAMES
    raise ValueError(f"Unsupported action encoding: {encoding_version}")


def _action_query_context(
    action: IndexAction,
    sql_text: str | None,
) -> list[float]:
    if action.kind is IndexActionKind.NOOP or not sql_text:
        return [0.0] * 8

    from sqlglot import exp, parse_one

    tree = parse_one(sql_text, read="postgres")
    aliases = {
        table.alias_or_name
        for table in tree.find_all(exp.Table)
        if (table.db or "public") == action.schema_name
        and table.name == action.table_name
    }
    physical_tables = [
        table
        for table in tree.find_all(exp.Table)
        if (table.db or "public") == action.schema_name
    ]
    allow_unqualified = len(physical_tables) == 1

    def referenced_columns(nodes) -> set[str]:
        return {
            column.name
            for node in nodes
            for column in node.find_all(exp.Column)
            if column.table in aliases or (not column.table and allow_unqualified)
        }

    where_columns = referenced_columns(tree.find_all(exp.Where))
    join_columns = referenced_columns(
        condition
        for join in tree.find_all(exp.Join)
        if (condition := join.args.get("on")) is not None
    )
    order_columns = referenced_columns(tree.find_all(exp.Order))
    select_columns = referenced_columns(
        expression
        for select in tree.find_all(exp.Select)
        for expression in select.expressions
    )
    leading = action.key_columns[0]
    table_references = sum(
        1
        for table in tree.find_all(exp.Table)
        if (table.db or "public") == action.schema_name
        and table.name == action.table_name
    )
    return [
        float(sum(column in where_columns for column in action.key_columns)),
        float(sum(column in join_columns for column in action.key_columns)),
        float(sum(column in order_columns for column in action.key_columns)),
        float(sum(column in select_columns for column in action.include_columns)),
        float(leading in where_columns),
        float(leading in join_columns),
        float(leading in order_columns),
        float(table_references),
    ]


def _hashed_columns(columns: tuple[str, ...]) -> list[float]:
    result = [0.0] * COLUMN_HASH_BUCKETS
    for position, column in enumerate(columns, start=1):
        digest = hashlib.sha256(column.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % COLUMN_HASH_BUCKETS
        result[bucket] += 1.0 / position
    return result


def _hashed_table(table_name: str | None) -> list[float]:
    result = [0.0] * TABLE_HASH_BUCKETS
    if table_name:
        digest = hashlib.sha256(table_name.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % TABLE_HASH_BUCKETS
        result[bucket] = 1.0
    return result


def collect_action_database_context(
    action: IndexAction,
    settings: DatabaseSettings | None = None,
    connection=None,
) -> dict[str, int | float | bool]:
    """Collect optimizer metadata for the table targeted by an action."""
    empty = {
        "target_relation_rows": 0.0,
        "target_relation_size_bytes": 0,
        "target_index_count": 0,
        "exact_index_exists": False,
        "prefix_index_exists": False,
    }
    if action.kind is IndexActionKind.NOOP:
        return empty

    import psycopg

    settings = settings or DatabaseSettings.from_env()
    owns_connection = connection is None
    connection = connection or psycopg.connect(**settings.connection_kwargs())
    try:
        relation = connection.execute(
            """
            SELECT c.oid, greatest(c.reltuples, 0)::double precision,
                   pg_total_relation_size(c.oid)
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND c.relkind IN ('r', 'p')
            """,
            (action.schema_name, action.table_name),
        ).fetchone()
        if relation is None:
            return empty
        index_rows = connection.execute(
            """
            SELECT i.indnkeyatts,
                   array_agg(a.attname ORDER BY key.ordinality)
                       FILTER (WHERE key.ordinality <= i.indnkeyatts
                               AND a.attname IS NOT NULL)
            FROM pg_index AS i
            CROSS JOIN LATERAL unnest(i.indkey)
                WITH ORDINALITY AS key(attnum, ordinality)
            LEFT JOIN pg_attribute AS a
              ON a.attrelid = i.indrelid AND a.attnum = key.attnum
            WHERE i.indrelid = %s
            GROUP BY i.indexrelid, i.indnkeyatts
            """,
            (relation[0],),
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()

    wanted = tuple(action.key_columns)
    indexes = [tuple(row[1] or ()) for row in index_rows]
    return {
        "target_relation_rows": float(relation[1]),
        "target_relation_size_bytes": int(relation[2]),
        "target_index_count": len(indexes),
        "exact_index_exists": any(keys == wanted for keys in indexes),
        "prefix_index_exists": any(keys[: len(wanted)] == wanted for keys in indexes),
    }


def _optimizer_action_context(
    action: IndexAction,
    sql_text: str | None,
    database_context: dict[str, int | float | bool],
) -> list[float]:
    if action.kind is IndexActionKind.NOOP:
        return [0.0] * 7
    non_sargable = _non_sargable_action_columns(action, sql_text)
    return [
        math.log1p(max(0.0, float(database_context.get("target_relation_rows", 0)))),
        math.log1p(max(0.0, float(database_context.get("target_relation_size_bytes", 0)))),
        math.log1p(max(0.0, float(database_context.get("target_index_count", 0)))),
        float(bool(database_context.get("exact_index_exists", False))),
        float(bool(database_context.get("prefix_index_exists", False))),
        float(sum(column in non_sargable for column in action.key_columns)),
        float(bool(action.key_columns and action.key_columns[0] in non_sargable)),
    ]


def _non_sargable_action_columns(
    action: IndexAction,
    sql_text: str | None,
) -> set[str]:
    if not sql_text:
        return set()
    from sqlglot import exp, parse_one

    tree = parse_one(sql_text, read="postgres")
    aliases = {
        table.alias_or_name
        for table in tree.find_all(exp.Table)
        if (table.db or "public") == action.schema_name
        and table.name == action.table_name
    }
    physical_tables = [
        table
        for table in tree.find_all(exp.Table)
        if (table.db or "public") == action.schema_name
    ]
    allow_unqualified = len(physical_tables) == 1
    direct_predicates = (
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.Between,
        exp.In,
        exp.Is,
        exp.Like,
        exp.ILike,
    )
    occurrences: dict[str, list[bool]] = {}
    predicate_nodes = list(tree.find_all(exp.Where))
    predicate_nodes.extend(
        condition
        for join in tree.find_all(exp.Join)
        if (condition := join.args.get("on")) is not None
    )
    for node in predicate_nodes:
        for column in node.find_all(exp.Column):
            if column.table not in aliases and not (
                not column.table and allow_unqualified
            ):
                continue
            parent = column.parent
            while isinstance(parent, exp.Paren):
                parent = parent.parent
            occurrences.setdefault(column.name, []).append(
                not isinstance(parent, direct_predicates)
            )
    return {
        column
        for column, flags in occurrences.items()
        if flags and all(flags)
    }
