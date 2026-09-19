"""Measured validation of structural advice with mandatory transaction rollback."""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median
from time import perf_counter
import secrets
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .sql_rewrite import _measure_pair, _measure_query, _queries_are_equivalent
from .structural_advisor import StructuralRecommendation


REWRITE_RULES = {
    "non-aggregate-having-filter": "having-filter-rewrite",
    "subquery-order-without-limit": "subquery-order-rewrite",
}
WORK_MEM_RULES = {
    "sort-spill-to-disk",
    "aggregate-spill-to-disk",
    "hash-join-multiple-batches",
}
MATERIALIZED_VIEW_RULE = "expensive-summary-materialization"


@dataclass(frozen=True, slots=True)
class StructuralValidationResult:
    recommendation_id: int | None
    rule_id: str
    supported: bool
    validation_method: str
    equivalent: bool | None
    baseline_time_ms: float | None
    candidate_time_ms: float | None
    improvement_ratio: float | None
    accepted: bool
    rolled_back: bool
    details: dict[str, Any]
    artifact_creation_time_ms: float | None = None
    validation_id: int | None = None


def automatic_validation_supported(rule_id: str) -> bool:
    return _validation_method(rule_id) is not None


def validate_and_save_structural_recommendation(
    sql_text: str,
    recommendation: StructuralRecommendation,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
    work_mem_mb: int = 64,
    minimum_baseline_time_ms: float = 0.0,
    minimum_absolute_improvement_ms: float = 5.0,
    minimum_improvement_ratio: float = 0.05,
) -> StructuralValidationResult:
    """Run a rolled-back trial, persist evidence, and update accepted feedback."""
    from .structural_feedback import record_recommendation_measurement

    result = validate_structural_recommendation(
        sql_text,
        recommendation,
        settings,
        repetitions=repetitions,
        work_mem_mb=work_mem_mb,
        minimum_baseline_time_ms=minimum_baseline_time_ms,
        minimum_absolute_improvement_ms=minimum_absolute_improvement_ms,
        minimum_improvement_ratio=minimum_improvement_ratio,
    )
    if not result.supported:
        return result
    if recommendation.recommendation_id is None:
        raise ValueError("Save the analysis before automatic validation")
    result = save_structural_validation(result, settings)
    note = (
        f"Автопроверка #{result.validation_id}: {result.validation_method}; "
        f"эквивалентность={result.equivalent}; rollback={result.rolled_back}"
    )
    record_recommendation_measurement(
        recommendation.recommendation_id,
        result.baseline_time_ms,
        result.candidate_time_ms,
        note,
        settings,
    )
    return result


def validate_structural_recommendation(
    sql_text: str,
    recommendation: StructuralRecommendation,
    settings: DatabaseSettings | None = None,
    *,
    repetitions: int = 3,
    work_mem_mb: int = 64,
    minimum_baseline_time_ms: float = 0.0,
    minimum_absolute_improvement_ms: float = 5.0,
    minimum_improvement_ratio: float = 0.05,
) -> StructuralValidationResult:
    """Execute a supported trial and always roll its transaction back."""
    import psycopg

    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    if not 1 <= work_mem_mb <= 256:
        raise ValueError("work_mem_mb must be between 1 and 256")
    if min(
        minimum_baseline_time_ms,
        minimum_absolute_improvement_ms,
        minimum_improvement_ratio,
    ) < 0:
        raise ValueError("validation thresholds must be non-negative")

    original_sql = _assert_read_only_query(sql_text)
    method = _validation_method(recommendation.rule_id)
    if method is None:
        return StructuralValidationResult(
            recommendation.recommendation_id,
            recommendation.rule_id,
            False,
            "unsupported",
            None,
            None,
            None,
            None,
            False,
            True,
            {
                "reason": "Rule has no semantics-preserving automatic trial",
                "manual_verification": recommendation.verification,
            },
        )

    settings = settings or DatabaseSettings.from_env()
    connection = psycopg.connect(**settings.connection_kwargs())
    try:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{settings.statement_timeout_ms}ms",),
        )
        if recommendation.rule_id in REWRITE_RULES:
            result = _validate_rewrite(
                connection,
                original_sql,
                recommendation,
                repetitions,
            )
        elif recommendation.rule_id in WORK_MEM_RULES:
            result = _validate_work_mem(
                connection,
                original_sql,
                recommendation,
                repetitions,
                work_mem_mb,
            )
        else:
            result = _validate_materialized_view(
                connection,
                original_sql,
                recommendation,
                repetitions,
            )
    finally:
        connection.rollback()
        connection.close()

    if result.baseline_time_ms is None or result.candidate_time_ms is None:
        return result
    absolute_gain = result.baseline_time_ms - result.candidate_time_ms
    decision_reason = _validation_decision_reason(
        equivalent=bool(result.equivalent),
        baseline_time_ms=result.baseline_time_ms,
        absolute_gain_ms=absolute_gain,
        improvement_ratio=result.improvement_ratio,
        minimum_baseline_time_ms=minimum_baseline_time_ms,
        minimum_absolute_improvement_ms=minimum_absolute_improvement_ms,
        minimum_improvement_ratio=minimum_improvement_ratio,
    )
    details = {
        **result.details,
        "absolute_gain_ms": absolute_gain,
        "minimum_baseline_time_ms": minimum_baseline_time_ms,
        "minimum_absolute_improvement_ms": minimum_absolute_improvement_ms,
        "minimum_improvement_ratio": minimum_improvement_ratio,
        "decision_reason": decision_reason,
    }
    return replace(
        result,
        accepted=decision_reason == "accepted",
        rolled_back=True,
        details=details,
    )


def _validation_decision_reason(
    *,
    equivalent: bool,
    baseline_time_ms: float,
    absolute_gain_ms: float,
    improvement_ratio: float,
    minimum_baseline_time_ms: float,
    minimum_absolute_improvement_ms: float,
    minimum_improvement_ratio: float,
) -> str:
    if not equivalent:
        return "result_mismatch"
    if baseline_time_ms < minimum_baseline_time_ms:
        return "below_runtime_threshold"
    if absolute_gain_ms < minimum_absolute_improvement_ms:
        return "absolute_gain_too_small"
    if improvement_ratio < minimum_improvement_ratio:
        return "relative_gain_too_small"
    return "accepted"


def save_structural_validation(
    result: StructuralValidationResult,
    settings: DatabaseSettings | None = None,
    *,
    connection=None,
) -> StructuralValidationResult:
    """Persist a completed supported trial after its experiment was rolled back."""
    import psycopg

    if not result.supported or result.recommendation_id is None:
        raise ValueError("Only supported persisted recommendations can be saved")
    if result.baseline_time_ms is None or result.candidate_time_ms is None:
        raise ValueError("Validation result has no measurements")
    settings = settings or DatabaseSettings.from_env()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned_connection:
            return save_structural_validation(
                result, settings=settings, connection=owned_connection
            )
    ready = connection.execute(
        "SELECT to_regclass('pqo.structural_validation') IS NOT NULL"
    ).fetchone()[0]
    if not ready:
        raise RuntimeError(
            "pqo.structural_validation is missing; apply "
            "database/init/008_structural_validation.sql"
        )
    validation_id = connection.execute(
        """
        INSERT INTO pqo.structural_validation (
            recommendation_id, validation_method, equivalent,
            baseline_time_ms, candidate_time_ms, improvement_ratio,
            artifact_creation_time_ms, accepted, rolled_back, details
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            result.recommendation_id,
            result.validation_method,
            result.equivalent,
            result.baseline_time_ms,
            result.candidate_time_ms,
            result.improvement_ratio,
            result.artifact_creation_time_ms,
            result.accepted,
            result.rolled_back,
            Jsonb(result.details),
        ),
    ).fetchone()[0]
    return replace(result, validation_id=validation_id)


def _validation_method(rule_id: str) -> str | None:
    if rule_id in REWRITE_RULES:
        return REWRITE_RULES[rule_id]
    if rule_id in WORK_MEM_RULES:
        return "local-work-mem-trial"
    if rule_id == MATERIALIZED_VIEW_RULE:
        return "transactional-materialized-view"
    return None


def _validate_rewrite(connection, original_sql, recommendation, repetitions):
    rewritten_sql = _structural_rewrite(original_sql, recommendation.rule_id)
    if rewritten_sql is None:
        return _unsupported_result(
            recommendation, "A safe rewrite could not be generated for this SQL"
        )
    equivalent = _queries_are_equivalent(connection, original_sql, rewritten_sql)
    if not equivalent:
        result = _unsupported_result(
            recommendation, "Generated rewrite changed the result multiset"
        )
        return replace(result, details={**result.details, "rewritten_sql": rewritten_sql})
    baseline, candidate = _measure_pair(
        connection, original_sql, rewritten_sql, repetitions
    )
    return _measured_result(
        recommendation,
        REWRITE_RULES[recommendation.rule_id],
        baseline,
        candidate,
        {"rewritten_sql": rewritten_sql},
    )


def _validate_work_mem(
    connection, original_sql, recommendation, repetitions, work_mem_mb
):
    original_work_mem = connection.execute("SHOW work_mem").fetchone()[0]
    baseline_times = []
    candidate_times = []
    _measure_query(connection, original_sql)
    for repetition in range(repetitions):
        ordered = (
            ((original_work_mem, baseline_times), (f"{work_mem_mb}MB", candidate_times))
            if repetition % 2 == 0
            else ((f"{work_mem_mb}MB", candidate_times), (original_work_mem, baseline_times))
        )
        for memory_value, destination in ordered:
            connection.execute(
                "SELECT set_config('work_mem', %s, true)", (memory_value,)
            )
            destination.append(_measure_query(connection, original_sql))
    baseline = float(median(baseline_times))
    candidate = float(median(candidate_times))
    return _measured_result(
        recommendation,
        "local-work-mem-trial",
        baseline,
        candidate,
        {
            "original_work_mem": original_work_mem,
            "trial_work_mem": f"{work_mem_mb}MB",
            "scope": "SET LOCAL; no global configuration change",
        },
    )


def _validate_materialized_view(
    connection, original_sql, recommendation, repetitions
):
    if recommendation.suggested_sql is None:
        return _unsupported_result(
            recommendation,
            "Filtered materialized-view advice must be generalized manually",
        )
    schema_name = f"pqo_trial_{secrets.token_hex(6)}"
    view_name = "summary"
    connection.execute(
        sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
    )
    create_statement = sql.SQL("CREATE MATERIALIZED VIEW {}.{} AS ").format(
        sql.Identifier(schema_name), sql.Identifier(view_name)
    ) + sql.SQL(original_sql)
    started = perf_counter()
    connection.execute(create_statement)
    creation_time_ms = (perf_counter() - started) * 1000.0
    candidate_sql = sql.SQL("SELECT * FROM {}.{}").format(
        sql.Identifier(schema_name), sql.Identifier(view_name)
    ).as_string(connection)
    equivalent = _queries_are_equivalent(connection, original_sql, candidate_sql)
    if not equivalent:
        result = _unsupported_result(
            recommendation, "Trial materialized view changed the result multiset"
        )
        return replace(result, artifact_creation_time_ms=creation_time_ms)
    baseline, candidate = _measure_pair(
        connection, original_sql, candidate_sql, repetitions
    )
    return _measured_result(
        recommendation,
        "transactional-materialized-view",
        baseline,
        candidate,
        {
            "trial_relation": f"{schema_name}.{view_name}",
            "refresh_cost_note": "creation time approximates initial refresh cost",
        },
        creation_time_ms,
    )


def _structural_rewrite(sql_text: str, rule_id: str) -> str | None:
    from sqlglot import exp, parse_one

    tree = parse_one(sql_text, read="postgres")
    if rule_id == "subquery-order-without-limit":
        changed = False
        for select in tree.find_all(exp.Select):
            if select is tree:
                continue
            if select.args.get("order") is not None and select.args.get("limit") is None:
                select.set("order", None)
                changed = True
        return tree.sql(dialect="postgres", pretty=True) if changed else None

    if rule_id != "non-aggregate-having-filter":
        return None
    having = tree.find(exp.Having)
    if having is None or not isinstance(having.parent, exp.Select):
        return None
    terms = _flatten_and(having.this, exp)
    movable = [
        term
        for term in terms
        if not any(isinstance(node, exp.AggFunc) for node in term.walk())
    ]
    retained = [term for term in terms if term not in movable]
    if not movable:
        return None
    select = having.parent
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
    return tree.sql(dialect="postgres", pretty=True)


def _flatten_and(node, exp):
    if isinstance(node, exp.And):
        return [*_flatten_and(node.this, exp), *_flatten_and(node.expression, exp)]
    return [node]


def _combine_and(nodes, exp):
    expression = nodes[0].copy()
    for node in nodes[1:]:
        expression = exp.and_(expression, node.copy())
    return expression


def _measured_result(
    recommendation,
    method,
    baseline,
    candidate,
    details,
    creation_time_ms=None,
):
    ratio = (baseline - candidate) / max(baseline, 0.001)
    return StructuralValidationResult(
        recommendation.recommendation_id,
        recommendation.rule_id,
        True,
        method,
        True,
        baseline,
        candidate,
        ratio,
        False,
        True,
        details,
        creation_time_ms,
    )


def _unsupported_result(recommendation, reason):
    return StructuralValidationResult(
        recommendation.recommendation_id,
        recommendation.rule_id,
        False,
        "unsupported",
        None,
        None,
        None,
        None,
        False,
        True,
        {"reason": reason, "manual_verification": recommendation.verification},
    )
