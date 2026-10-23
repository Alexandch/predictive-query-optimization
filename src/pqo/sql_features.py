"""Structural feature extraction from PostgreSQL SQL text."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError


EXTENDED_SQL_FEATURES = [
    "cte_count", "materialized_cte_count", "cte_reference_count",
    "window_function_count", "window_aggregate_count", "ranking_window_count",
    "window_partition_key_count", "window_order_key_count",
    "distinct_aggregate_count", "aggregate_filter_count", "group_key_count",
]


@dataclass(frozen=True, slots=True)
class SQLFeatures:
    query_length: int
    join_count: int
    where_condition_count: int
    subquery_count: int
    has_group_by: bool
    has_order_by: bool
    has_distinct: bool
    table_reference_count: int
    unique_table_count: int
    select_expression_count: int
    aggregate_function_count: int
    inner_join_count: int
    left_join_count: int
    right_join_count: int
    full_join_count: int
    cross_join_count: int
    cte_count: int = 0
    materialized_cte_count: int = 0
    cte_reference_count: int = 0
    window_function_count: int = 0
    window_aggregate_count: int = 0
    ranking_window_count: int = 0
    window_partition_key_count: int = 0
    window_order_key_count: int = 0
    distinct_aggregate_count: int = 0
    aggregate_filter_count: int = 0
    group_key_count: int = 0

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _condition_count(expression: exp.Expression) -> int:
    if isinstance(expression, (exp.And, exp.Or)):
        return _condition_count(expression.left) + _condition_count(expression.right)
    return 1


def _join_category(join: exp.Join) -> str:
    side = str(join.args.get("side") or "").upper()
    kind = str(join.args.get("kind") or "").upper()

    if side in {"LEFT", "RIGHT", "FULL"}:
        return side.lower()
    if kind == "CROSS":
        return "cross"
    return "inner"


def extract_sql_features(sql_text: str) -> SQLFeatures:
    normalized = sql_text.strip()
    if not normalized:
        raise ValueError("SQL query must not be empty")

    try:
        statements = [statement for statement in parse(normalized, read="postgres") if statement]
    except ParseError as exc:
        raise ValueError(f"Invalid PostgreSQL SQL: {exc}") from exc

    if len(statements) != 1:
        raise ValueError("Exactly one SQL statement is required")

    tree = statements[0]
    joins = list(tree.find_all(exp.Join))
    join_categories = [_join_category(join) for join in joins]
    where_clauses = list(tree.find_all(exp.Where))
    select_nodes = list(tree.find_all(exp.Select))
    cte_names = {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    table_names = [
        table.name.lower()
        for table in tree.find_all(exp.Table)
        if table.name and table.name.lower() not in cte_names
    ]
    windows = list(tree.find_all(exp.Window))
    aggregates = list(tree.find_all(exp.AggFunc))

    return SQLFeatures(
        query_length=len(normalized),
        join_count=len(joins),
        where_condition_count=sum(
            _condition_count(where.this) for where in where_clauses
        ),
        # Every SELECT below the statement's outer SELECT is a subquery. This
        # also counts EXISTS/IN subqueries, which SQLGlot doesn't always wrap
        # in an explicit Subquery node.
        subquery_count=max(0, len(select_nodes) - 1),
        has_group_by=any(True for _ in tree.find_all(exp.Group)),
        has_order_by=any(True for _ in tree.find_all(exp.Order)),
        has_distinct=any(select.args.get("distinct") is not None for select in select_nodes),
        table_reference_count=len(table_names),
        unique_table_count=len(set(table_names)),
        select_expression_count=sum(len(select.expressions) for select in select_nodes),
        aggregate_function_count=sum(1 for _ in tree.find_all(exp.AggFunc)),
        inner_join_count=join_categories.count("inner"),
        left_join_count=join_categories.count("left"),
        right_join_count=join_categories.count("right"),
        full_join_count=join_categories.count("full"),
        cross_join_count=join_categories.count("cross"),
        cte_count=sum(1 for _ in tree.find_all(exp.CTE)),
        materialized_cte_count=sum(cte.args.get("materialized") is True for cte in tree.find_all(exp.CTE)),
        cte_reference_count=sum(table.name.lower() in cte_names for table in tree.find_all(exp.Table)),
        window_function_count=len(windows),
        window_aggregate_count=sum(
            isinstance(window.this, exp.AggFunc) and window.this.key not in {
                "rank", "denserank", "rownumber", "lag", "lead", "ntile",
                "firstvalue", "lastvalue", "nthvalue", "percentrank", "cumedist",
            } for window in windows
        ),
        ranking_window_count=sum(isinstance(window.this, (exp.Rank, exp.DenseRank, exp.RowNumber)) for window in windows),
        window_partition_key_count=sum(len(window.args.get("partition_by") or []) for window in windows),
        window_order_key_count=sum(len(order.expressions) if (order := window.args.get("order")) is not None else 0 for window in windows),
        distinct_aggregate_count=sum(any(isinstance(arg, exp.Distinct) for arg in aggregate.iter_expressions()) for aggregate in aggregates),
        aggregate_filter_count=sum(isinstance(item.this, exp.AggFunc) for item in tree.find_all(exp.Filter)),
        group_key_count=sum(len(group.expressions) for group in tree.find_all(exp.Group)),
    )
