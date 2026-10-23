"""Semantics-checked and measured SQL rewrite recommendations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
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
    journal_run_id: str | None = None
    journal_warning: str | None = None


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
            "count-zero-to-not-exists",
            "Заменить проверку COUNT(*) = 0 на NOT EXISTS",
            _rewrite_zero_counts,
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
        (
            "subquery-order-without-limit",
            "Удалить лишний ORDER BY из подзапроса без LIMIT",
            _rewrite_subquery_orders,
        ),
        (
            "inline-reused-cte",
            "Проверить NOT MATERIALIZED для повторно используемого CTE",
            _rewrite_reused_ctes,
        ),
        (
            "non-aggregate-having-filter",
            "Перенести неагрегатный фильтр из HAVING в WHERE",
            _rewrite_non_aggregate_having,
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
    sql_text: str, settings: DatabaseSettings | None = None, *,
    repetitions: int = 3, minimum_baseline_time_ms: float = 50.0,
    minimum_absolute_improvement_ms: float = 5.0, minimum_improvement_ratio: float = 0.05,
    _candidates: tuple[SQLRewriteCandidate, ...] | None = None,
    journal_directory: str | Path | None = None,
    journal_source: str = "api",
    journal_metadata: dict | None = None,
) -> SQLRewritePlan:
    """Optionally record the entire experiment, including failures and skipped checks."""
    from .rewrite_journal import RewriteJournalRun, error_record
    settings = settings or DatabaseSettings.from_env()
    recorder = None
    if journal_directory is not None:
        recorder = RewriteJournalRun(
            journal_directory, sql_text, settings,
            mode="automatic" if _candidates is None else "manual",
            source=journal_source,
            metadata=journal_metadata,
            repetitions=repetitions,
            thresholds={"baseline_ms": minimum_baseline_time_ms,
                        "absolute_ms": minimum_absolute_improvement_ms,
                        "relative": minimum_improvement_ratio},
        )
    try:
        result = _evaluate_sql_rewrites(
            sql_text, settings, repetitions=repetitions,
            minimum_baseline_time_ms=minimum_baseline_time_ms,
            minimum_absolute_improvement_ms=minimum_absolute_improvement_ms,
            minimum_improvement_ratio=minimum_improvement_ratio,
            _candidates=_candidates, _recorder=recorder,
        )
    except Exception as exc:
        if recorder:
            recorder.finish(error=error_record(exc, recorder.data["stage"]))
            if recorder.warning:
                exc.add_note(recorder.warning)
        raise
    if recorder:
        recorder.finish(result)
        result = replace(result, journal_run_id=recorder.data["run_id"], journal_warning=recorder.warning)
    return result


def _evaluate_sql_rewrites(
    sql_text: str,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
    minimum_baseline_time_ms: float = 50.0,
    minimum_absolute_improvement_ms: float = 5.0,
    minimum_improvement_ratio: float = 0.05,
    _candidates: tuple[SQLRewriteCandidate, ...] | None = None,
    _recorder=None,
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
    candidates = generate_sql_rewrites(original_sql) if _candidates is None else _candidates
    if _recorder:
        _recorder.candidates(candidates)
    for candidate in candidates:
        _assert_read_only_query(candidate.sql_text)
    if _candidates is not None:
        from sqlglot import parse_one
        if any(parse_one(original_sql, read="postgres") == parse_one(item.sql_text, read="postgres") for item in candidates):
            raise ValueError("Вариант SQL совпадает с исходным: изменилось только оформление")
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
    if _recorder:
        _recorder.stage("connection")
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{settings.statement_timeout_ms}ms",),
            )
            if _recorder:
                safe_settings = ["work_mem", "jit", "max_parallel_workers_per_gather",
                                 "random_page_cost", "seq_page_cost", "effective_cache_size"]
                _recorder.data["environment"] = {"postgres_version": connection.info.server_version}
                if _recorder.enabled:
                    try:
                        with connection.transaction():
                            _recorder.data["environment"]["settings"] = {
                                name: {"value": value, "unit": unit} for name, value, unit in connection.execute(
                                    "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)", (safe_settings,)
                                ).fetchall()
                            }
                    except Exception as exc:
                        from .rewrite_journal import error_record
                        _recorder.data["environment"]["settings_error"] = error_record(exc, "settings_metadata")
                _recorder.stage("baseline_probe")
            baseline_probe = _measure_query(connection, original_sql,
                observer=(lambda time, plan: _recorder.measurement(None, "baseline", "probe", time, plan)) if _recorder else None)
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

            if _recorder:
                _recorder.stage("candidates")
            for index, candidate in enumerate(candidates):
                try:
                    with connection.transaction():
                        if _recorder:
                            _recorder.candidate_stage(index, "estimated_plans")
                            _recorder.data["candidates"][index]["estimated_plans"] = {
                                "baseline": connection.execute(f"EXPLAIN (FORMAT JSON) {original_sql}").fetchone()[0],
                                "variant": connection.execute(f"EXPLAIN (FORMAT JSON) {candidate.sql_text}").fetchone()[0],
                            }
                            _recorder.candidate_stage(index, "equivalence")
                        equivalent = _queries_are_equivalent(
                            connection, original_sql, candidate.sql_text
                        )
                        if _recorder:
                            _recorder.data["candidates"][index]["equivalence"] = equivalent
                            _recorder.flush()
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
                            if _recorder:
                                _recorder.evaluation(index, evaluations[-1])
                            continue
                        if _recorder:
                            _recorder.candidate_stage(index, "measurement")
                        baseline_time, rewritten_time = _measure_pair(
                            connection,
                            original_sql,
                            candidate.sql_text,
                            repetitions,
                            observer=(lambda side, phase, time, plan: _recorder.measurement(index, side, phase, time, plan)) if _recorder else None,
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
                    if _recorder:
                        from .rewrite_journal import error_record
                        _recorder.evaluation(index, evaluations[-1], error_record(exc, _recorder.data["candidates"][index]["stage"]))
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
                if _recorder:
                    _recorder.evaluation(index, evaluations[-1])

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
        ("recommended" if recommended is not None else
         "no_measured_improvement" if any(item.equivalent for item in evaluations) else
         "result_mismatch" if any(item.rejection_reason == "result_mismatch" for item in evaluations) else
         "verification_failed"),
    )


def evaluate_sql_variant(original_sql: str, variant_sql: str,
                         settings: DatabaseSettings | None = None, **kwargs) -> SQLRewritePlan:
    """Verify and measure an explicitly supplied SELECT without replacing the source."""
    return evaluate_sql_rewrites(
        original_sql, settings, _candidates=(SQLRewriteCandidate(
            "editor-variant", "Вариант из правого редактора", variant_sql),), **kwargs,
    )


def _rewrite_zero_counts(expression):
    from sqlglot import exp
    changed = False
    def transform(node):
        nonlocal changed
        if not isinstance(node, exp.EQ):
            return node
        subquery = (node.this if _is_number(node.expression, 0) else
                    node.expression if _is_number(node.this, 0) else None)
        if not isinstance(subquery, exp.Subquery) or not _is_simple_count_star(subquery.this):
            return node
        select = subquery.this.copy()
        select.set("expressions", [exp.Literal.number(1)])
        select.set("order", None)
        changed = True
        return exp.Not(this=exp.Exists(this=select))
    return expression.transform(transform), changed


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


def _rewrite_subquery_orders(expression):
    """Remove ordering that cannot affect the outer query result."""
    from sqlglot import exp

    changed = False
    for select in expression.find_all(exp.Select):
        if select is expression:
            continue
        distinct = select.args.get("distinct")
        if (select.args.get("order") is not None
                and select.args.get("limit") is None
                and select.args.get("offset") is None
                and not (distinct is not None and distinct.args.get("on") is not None)):
            select.set("order", None)
            changed = True
    return expression, changed


def _rewrite_reused_ctes(expression):
    """Ask PostgreSQL to inline eligible SELECT CTEs; measure instead of assuming a win."""
    from sqlglot import exp
    if any(with_clause.args.get("recursive") for with_clause in expression.find_all(exp.With)):
        return expression, False
    changed = False
    tables = list(expression.find_all(exp.Table))
    for cte in expression.find_all(exp.CTE):
        references = sum(not table.db and table.name == cte.alias_or_name for table in tables)
        if references > 1 and isinstance(cte.this, exp.Select) and cte.args.get("materialized") is not False:
            cte.set("materialized", False)
            changed = True
    return expression, changed


def _rewrite_non_aggregate_having(expression):
    """Move safe non-aggregate HAVING terms into WHERE."""
    from sqlglot import exp

    changed = False
    for select in expression.find_all(exp.Select):
        if select.args.get("group") is None:
            continue
        having = select.args.get("having")
        if having is None:
            continue
        terms = _flatten_boolean(having.this, exp.And)
        movable = [
            term
            for term in terms
            if not any(isinstance(node, exp.AggFunc) for node in term.walk())
        ]
        retained = [term for term in terms if term not in movable]
        if not movable:
            continue
        movable_expression = _combine_and(movable, exp)
        existing_where = select.args.get("where")
        where_expression = (
            exp.and_(existing_where.this, movable_expression)
            if existing_where is not None
            else movable_expression
        )
        select.set("where", exp.Where(this=where_expression))
        select.set(
            "having",
            exp.Having(this=_combine_and(retained, exp)) if retained else None,
        )
        changed = True
    return expression, changed


def _combine_and(nodes, exp):
    combined = nodes[0].copy()
    for node in nodes[1:]:
        combined = exp.and_(combined, node.copy())
    return combined


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


def _measure_pair(connection, original_sql: str, rewritten_sql: str, repetitions: int, observer=None):
    def measure(sql, side, phase):
        return _measure_query(connection, sql,
            observer=(lambda time, plan: observer(side, phase, time, plan)) if observer else None)
    measure(original_sql, "baseline", "warmup")
    measure(rewritten_sql, "variant", "warmup")
    baseline_times = []
    rewritten_times = []
    for repetition in range(repetitions):
        ordered = (
            ((original_sql, baseline_times, "baseline"), (rewritten_sql, rewritten_times, "variant"))
            if repetition % 2 == 0
            else ((rewritten_sql, rewritten_times, "variant"), (original_sql, baseline_times, "baseline"))
        )
        for query, destination, side in ordered:
            destination.append(measure(query, side, "measurement"))
    return float(median(baseline_times)), float(median(rewritten_times))


def _measure_query(connection, sql_text: str, observer=None) -> float:
    row = connection.execute(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql_text}"
    ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL returned no EXPLAIN result")
    actual_time = extract_plan_features(row[0]).actual_total_time_ms
    if actual_time is None:
        raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
    if observer:
        observer(float(actual_time), row[0])
    return float(actual_time)
