"""Semantics-checked and measured SQL rewrite recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .plan_features import extract_plan_features


@dataclass(frozen=True, slots=True)
class SQLRewriteCandidate:
    rule_id: str
    title: str
    sql_text: str


@dataclass(frozen=True, slots=True)
class SQLRewriteEvaluation:
    candidate: SQLRewriteCandidate
    equivalent: bool
    baseline_time_ms: float | None
    rewritten_time_ms: float | None
    improvement_ratio: float | None
    accepted: bool
    rejection_reason: str | None

    @property
    def absolute_improvement_ms(self) -> float | None:
        if self.baseline_time_ms is None or self.rewritten_time_ms is None:
            return None
        return self.baseline_time_ms - self.rewritten_time_ms


@dataclass(frozen=True, slots=True)
class SQLRewritePlan:
    original_sql: str
    evaluations: tuple[SQLRewriteEvaluation, ...]
    recommended: SQLRewriteEvaluation | None
    minimum_baseline_time_ms: float
    minimum_absolute_improvement_ms: float
    minimum_improvement_ratio: float
    terminal_reason: str


def generate_sql_rewrites(sql_text: str) -> tuple[SQLRewriteCandidate, ...]:
    """Generate conservative PostgreSQL rewrites without executing SQL."""
    from sqlglot import parse_one

    normalized = _assert_read_only_query(sql_text)
    parsed = parse_one(normalized, read="postgres")
    rules = (
        (
            "count-positive-to-exists",
            "Заменить проверку COUNT(*) > 0 на EXISTS",
            _rewrite_positive_counts,
        ),
        (
            "or-equality-to-in",
            "Заменить цепочку OR по одному столбцу на IN",
            _rewrite_or_equalities,
        ),
        (
            "range-to-between",
            "Заменить две границы диапазона на BETWEEN",
            _rewrite_ranges,
        ),
    )
    candidates = []
    seen_sql = {normalized}
    for rule_id, title, rewrite in rules:
        rewritten, changed = rewrite(parsed.copy())
        if not changed:
            continue
        rendered = rewritten.sql(dialect="postgres", pretty=True)
        if rendered in seen_sql:
            continue
        _assert_read_only_query(rendered)
        seen_sql.add(rendered)
        candidates.append(SQLRewriteCandidate(rule_id, title, rendered))
    return tuple(candidates)


def evaluate_sql_rewrites(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
    minimum_baseline_time_ms: float = 50.0,
    minimum_absolute_improvement_ms: float = 5.0,
    minimum_improvement_ratio: float = 0.05,
) -> SQLRewritePlan:
    """Prove result equivalence and measure rewrite candidates in PostgreSQL."""
    import psycopg

    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    if min(
        minimum_baseline_time_ms,
        minimum_absolute_improvement_ms,
        minimum_improvement_ratio,
    ) < 0:
        raise ValueError("rewrite thresholds must be non-negative")

    settings = settings or DatabaseSettings.from_env()
    original_sql = _assert_read_only_query(sql_text)
    candidates = generate_sql_rewrites(original_sql)
    if not candidates:
        return SQLRewritePlan(
            original_sql,
            (),
            None,
            minimum_baseline_time_ms,
            minimum_absolute_improvement_ms,
            minimum_improvement_ratio,
            "no_candidates",
        )

    evaluations = []
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{settings.statement_timeout_ms}ms",),
            )
            baseline_probe = _measure_query(connection, original_sql)
            if baseline_probe < minimum_baseline_time_ms:
                return SQLRewritePlan(
                    original_sql,
                    (),
                    None,
                    minimum_baseline_time_ms,
                    minimum_absolute_improvement_ms,
                    minimum_improvement_ratio,
                    "below_runtime_threshold",
                )

            for candidate in candidates:
                try:
                    with connection.transaction():
                        equivalent = _queries_are_equivalent(
                            connection, original_sql, candidate.sql_text
                        )
                        if not equivalent:
                            evaluations.append(
                                SQLRewriteEvaluation(
                                    candidate,
                                    False,
                                    None,
                                    None,
                                    None,
                                    False,
                                    "result_mismatch",
                                )
                            )
                            continue
                        baseline_time, rewritten_time = _measure_pair(
                            connection,
                            original_sql,
                            candidate.sql_text,
                            repetitions,
                        )
                except Exception as exc:
                    evaluations.append(
                        SQLRewriteEvaluation(
                            candidate,
                            False,
                            None,
                            None,
                            None,
                            False,
                            f"verification_error: {type(exc).__name__}: {exc}",
                        )
                    )
                    continue

                absolute_gain = baseline_time - rewritten_time
                improvement_ratio = absolute_gain / max(baseline_time, 0.001)
                accepted = (
                    absolute_gain >= minimum_absolute_improvement_ms
                    and improvement_ratio >= minimum_improvement_ratio
                )
                evaluations.append(
                    SQLRewriteEvaluation(
                        candidate,
                        True,
                        baseline_time,
                        rewritten_time,
                        improvement_ratio,
                        accepted,
                        None if accepted else "improvement_too_small",
                    )
                )

    accepted_evaluations = [item for item in evaluations if item.accepted]
    recommended = (
        max(
            accepted_evaluations,
            key=lambda item: item.absolute_improvement_ms or 0.0,
        )
        if accepted_evaluations
        else None
    )
    return SQLRewritePlan(
        original_sql,
        tuple(evaluations),
        recommended,
        minimum_baseline_time_ms,
        minimum_absolute_improvement_ms,
        minimum_improvement_ratio,
        "recommended" if recommended is not None else "no_measured_improvement",
    )


def _rewrite_or_equalities(expression):
    from sqlglot import exp

    changed = False

    def transform(node):
        nonlocal changed
        if not isinstance(node, exp.Or):
            return node
        terms = _flatten_boolean(node, exp.Or)
        pairs = [_column_value_equality(term) for term in terms]
        if any(pair is None for pair in pairs):
            return node
        column_sql = pairs[0][0].sql(dialect="postgres")
        if any(pair[0].sql(dialect="postgres") != column_sql for pair in pairs[1:]):
            return node
        changed = True
        return exp.In(
            this=pairs[0][0].copy(),
            expressions=[pair[1].copy() for pair in pairs],
        )

    return expression.transform(transform), changed


def _rewrite_positive_counts(expression):
    from sqlglot import exp

    changed = False

    def transform(node):
        nonlocal changed
        subquery = None
        if isinstance(node, exp.GT):
            if _is_number(node.expression, 0) and isinstance(node.this, exp.Subquery):
                subquery = node.this
        elif isinstance(node, exp.LT):
            if _is_number(node.this, 0) and isinstance(node.expression, exp.Subquery):
                subquery = node.expression
        elif isinstance(node, exp.GTE):
            if _is_number(node.expression, 1) and isinstance(node.this, exp.Subquery):
                subquery = node.this
        elif isinstance(node, exp.LTE):
            if _is_number(node.this, 1) and isinstance(node.expression, exp.Subquery):
                subquery = node.expression
        if subquery is None or not _is_simple_count_star(subquery.this):
            return node
        existence_query = subquery.this.copy()
        existence_query.set("expressions", [exp.Literal.number(1)])
        existence_query.set("order", None)
        changed = True
        return exp.Exists(this=existence_query)

    return expression.transform(transform), changed


def _rewrite_ranges(expression):
    from sqlglot import exp

    changed = False

    def transform(node):
        nonlocal changed
        if not isinstance(node, exp.And):
            return node
        terms = _flatten_boolean(node, exp.And)
        if len(terms) != 2:
            return node
        bounds = [_column_bound(term) for term in terms]
        if any(bound is None for bound in bounds):
            return node
        if bounds[0][0].sql(dialect="postgres") != bounds[1][0].sql(
            dialect="postgres"
        ):
            return node
        lower = next((bound[2] for bound in bounds if bound[1] == "lower"), None)
        upper = next((bound[2] for bound in bounds if bound[1] == "upper"), None)
        if lower is None or upper is None:
            return node
        changed = True
        return exp.Between(
            this=bounds[0][0].copy(),
            low=lower.copy(),
            high=upper.copy(),
        )

    return expression.transform(transform), changed


def _flatten_boolean(node, expression_type):
    if isinstance(node, expression_type):
        return [
            *_flatten_boolean(node.this, expression_type),
            *_flatten_boolean(node.expression, expression_type),
        ]
    return [node]


def _column_value_equality(node):
    from sqlglot import exp

    if not isinstance(node, exp.EQ):
        return None
    if isinstance(node.this, exp.Column) and _is_constant(node.expression):
        return node.this, node.expression
    if isinstance(node.expression, exp.Column) and _is_constant(node.this):
        return node.expression, node.this
    return None


def _column_bound(node):
    from sqlglot import exp

    mapping = {exp.GTE: "lower", exp.LTE: "upper"}
    for expression_type, kind in mapping.items():
        if isinstance(node, expression_type):
            if isinstance(node.this, exp.Column) and _is_constant(node.expression):
                return node.this, kind, node.expression
            if isinstance(node.expression, exp.Column) and _is_constant(node.this):
                reversed_kind = "upper" if kind == "lower" else "lower"
                return node.expression, reversed_kind, node.this
    return None


def _is_constant(node) -> bool:
    from sqlglot import exp

    if isinstance(node, (exp.Literal, exp.Placeholder)):
        return True
    if isinstance(node, (exp.Cast, exp.Neg)):
        return _is_constant(node.this)
    return False


def _is_number(node, value: int) -> bool:
    from sqlglot import exp

    return (
        isinstance(node, exp.Literal)
        and not node.is_string
        and node.this == str(value)
    )


def _is_simple_count_star(select) -> bool:
    from sqlglot import exp

    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return False
    projection = select.expressions[0]
    if not isinstance(projection, exp.Count) or not isinstance(
        projection.this, exp.Star
    ):
        return False
    return not any(
        select.args.get(name)
        for name in ("distinct", "group", "having", "limit", "offset", "qualify")
    )


def _queries_are_equivalent(connection, original_sql: str, rewritten_sql: str) -> bool:
    comparison = f"""
        SELECT NOT EXISTS (
            SELECT 1
            FROM (
                (({original_sql}) EXCEPT ALL ({rewritten_sql}))
                UNION ALL
                (({rewritten_sql}) EXCEPT ALL ({original_sql}))
            ) AS difference
        )
    """
    return bool(connection.execute(comparison).fetchone()[0])


def _measure_pair(connection, original_sql: str, rewritten_sql: str, repetitions: int):
    _measure_query(connection, original_sql)
    _measure_query(connection, rewritten_sql)
    baseline_times = []
    rewritten_times = []
    for repetition in range(repetitions):
        ordered = (
            ((original_sql, baseline_times), (rewritten_sql, rewritten_times))
            if repetition % 2 == 0
            else ((rewritten_sql, rewritten_times), (original_sql, baseline_times))
        )
        for query, destination in ordered:
            destination.append(_measure_query(connection, query))
    return float(median(baseline_times)), float(median(rewritten_times))


def _measure_query(connection, sql_text: str) -> float:
    row = connection.execute(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql_text}"
    ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL returned no EXPLAIN result")
    actual_time = extract_plan_features(row[0]).actual_total_time_ms
    if actual_time is None:
        raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
    return float(actual_time)
