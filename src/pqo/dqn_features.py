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
COLUMN_HASH_BUCKETS = 24

STATE_FEATURE_NAMES = tuple(NUMERIC_FEATURES) + tuple(
    f"root_node_{name}" for name in ROOT_NODE_TYPES
)
ACTION_FEATURE_NAMES = (
    "is_noop",
    "is_create",
    "key_count",
    "include_count",
    "is_composite",
    "is_covering",
    *(f"table_{name}" for name in AVIATION_TABLES),
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


def encode_action(action: IndexAction, sql_text: str | None = None) -> list[float]:
    table_name = action.table_name or "other"
    if table_name not in AVIATION_TABLES:
        table_name = "other"

    prefix = [
        float(action.kind is IndexActionKind.NOOP),
        float(action.kind is IndexActionKind.CREATE),
        float(len(action.key_columns)),
        float(len(action.include_columns)),
        float(len(action.key_columns) > 1),
        float(bool(action.include_columns)),
    ]
    tables = [float(table_name == name) for name in AVIATION_TABLES]
    keys = _hashed_columns(action.key_columns)
    includes = _hashed_columns(action.include_columns)
    context = _action_query_context(action, sql_text)
    return [*prefix, *tables, *keys, *includes, *context]


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
