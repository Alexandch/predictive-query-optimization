"""Stable numeric representations of query states and index actions."""

from __future__ import annotations

import hashlib
import math

from .config import DatabaseSettings
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
)


def build_query_state(
    sql_text: str,
    settings: DatabaseSettings | None = None,
) -> list[float]:
    sql_features = extract_sql_features(sql_text).as_dict()
    plan_features = collect_explain(
        sql_text, settings=settings, analyze=False
    ).features.as_dict()
    return encode_query_state({**sql_features, **plan_features})


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


def encode_action(action: IndexAction) -> list[float]:
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
    return [*prefix, *tables, *keys, *includes]


def _hashed_columns(columns: tuple[str, ...]) -> list[float]:
    result = [0.0] * COLUMN_HASH_BUCKETS
    for position, column in enumerate(columns, start=1):
        digest = hashlib.sha256(column.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % COLUMN_HASH_BUCKETS
        result[bucket] += 1.0 / position
    return result
