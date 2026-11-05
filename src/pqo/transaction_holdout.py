"""Frozen index hypothesis checked on new explicit SELECTs; never selection on control."""
from dataclasses import dataclass
import time

from sqlglot import exp, parse, parse_one

from .analysis_control import AnalysisCancelled, AnalysisControl
from .transaction_comparison import _baseline_problem, source_context_digest, sql_digest
from .transaction_index_trial import _read_slice, run_transaction_index_set_trial
from .transaction_inspection import inspect_transaction


@dataclass(frozen=True)
class FrozenIndexSelection:
    actions: tuple
    copy_database: str
    variant_number: int
    development_stable: bool
    development_queries: tuple[str, ...]
    development_sql_digest: str
    source_context_digest: str
    minimum_gain_ms: float
    minimum_gain_ratio: float


@dataclass(frozen=True)
class HoldoutWorkload:
    sql_text: str  # validated BEGIN/SELECT-only/ROLLBACK wrapper, no user context commands
    queries: tuple[str, ...]


@dataclass(frozen=True)
class TransactionHoldoutReport:
    selection: FrozenIndexSelection
    workload: HoldoutWorkload
    trials: tuple
    completed: bool
    passed_for_reads: bool
    reasons: tuple[str, ...]

    @property
    def validated_on_both(self):
        return self.selection.development_stable and self.passed_for_reads


def _query_key(query):
    # Exact normalized AST text only, NOT semantic equivalence or disjoint rows.
    return parse_one(query, read="postgres").sql(dialect="postgres", normalize=True, comments=False)


def freeze_index_selection(comparison, row, development_sql, settings):
    """Called BEFORE revealing the control editor. No DB connection, no SQL execution."""
    if (comparison is None or not comparison.completed or not comparison.trials
            or any(not trial.rollback_verified or trial.status in {"error", "cancelled"} for trial in comparison.trials)
            or type(row) is not int or not 0 <= row < len(comparison.trials)):
        raise ValueError("Нужно полное сравнение с подтверждёнными откатами; выберите строку измеренного варианта.")
    if (comparison.development_sql_digest != sql_digest(development_sql)
            or comparison.source_context_digest != source_context_digest(settings)):
        raise ValueError("Сравнение не связано с текущим SQL/подключением. Выполните сравнение заново.")
    trial = comparison.trials[row]
    if trial.status != "measured" or not trial.accepted_for_reads or not trial.readings:
        raise ValueError("Выбранная проба не подтвердила пользу для чтений. Контроль не подменяет результат подбора.")
    if comparison.copy_database != trial.copy_database:
        raise ValueError("Копия сравнения и выбранной пробы не совпадает.")
    queries, _ = _read_slice(development_sql, settings, trial.actions[0])
    preferred = comparison.preferred_variant
    stable = comparison.stable_for_reads and preferred is not None and comparison.trials[preferred].actions == trial.actions
    return FrozenIndexSelection(trial.actions, trial.copy_database, comparison.coordinate(row)[1], stable,
        tuple(_query_key(query) for _, query in queries), comparison.development_sql_digest,
        comparison.source_context_digest, trial.minimum_gain_ms, trial.minimum_gain_ratio)


def prepare_holdout(sql_text, selection, settings):
    """1..20 concrete SELECTs, all new relative to development; refuse before DB."""
    if (not isinstance(sql_text, str) or not sql_text.strip() or len(sql_text) > 500_000 or "\x00" in sql_text):
        raise ValueError("Контроль: введите 1–20 SELECT, не более 500000 символов, без NUL.")
    if selection.source_context_digest != source_context_digest(settings):
        raise ValueError("Подключение/разрешённые схемы изменились после фиксации варианта.")
    try:
        trees = [node for node in parse(sql_text, read="postgres") if node is not None and not isinstance(node, exp.Semicolon)]
    except Exception as exc:
        raise ValueError("Контрольный SQL не разобран; проверьте синтаксис.") from exc
    if not 1 <= len(trees) <= 20 or any(not isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except)) for node in trees):
        raise ValueError("Контроль принимает только 1–20 самостоятельных SELECT, без BEGIN, DML, DDL и SET.")
    rendered = tuple(node.sql(dialect="postgres", comments=False) for node in trees)
    wrapped = "BEGIN;\n" + ";\n".join(rendered) + ";\nROLLBACK;"
    inspection = inspect_transaction(wrapped)
    if any(item.changes_data for item in inspection.statements):
        raise ValueError("Запись, SELECT INTO и изменяющие CTE в контроле запрещены, а не пропускаются.")
    # Reuse the same function/lock/schema/parameter guards as actual copy trials.
    for action in selection.actions:
        _read_slice(wrapped, settings, action)
    keys = tuple(_query_key(query) for query in rendered)
    if len(set(keys)) != len(keys):
        raise ValueError("Контроль содержит повторяющиеся SELECT после нормализации.")
    if set(keys) & set(selection.development_queries):
        raise ValueError("Контроль повторяет SELECT подбора после нормализации. Нужны новые запросы/значения.")
    return HoldoutWorkload(wrapped, keys)


def validate_index_holdout(selection, sql_text, settings, copy_database, *, test_copy_confirmed=False, control=None):
    """Two fixed-set A/B/A trials, 180s shared budget; never try another variant.

    Unit weights for control SELECTs. No development/control pooling, retraining,
    DML, permanent index application or claims about total transaction runtime.
    The GUI permits ONE actual attempt per frozen comparison. API is stateless:
    it cannot prove a user has never inspected/reused these control data before.
    """
    control = control or AnalysisControl()
    control.check()
    if (not test_copy_confirmed or not copy_database.strip()
            or copy_database.strip() != selection.copy_database
            or copy_database.strip().casefold() == settings.dbname.casefold()):
        raise ValueError("Нужна та же подтверждённая отдельная тестовая копия, что при подборе.")
    workload = prepare_holdout(sql_text, selection, settings)
    trials, reasons = [], []
    deadline = time.monotonic() + 180
    for series in range(2):
        if control.cancelled.is_set() or time.monotonic() >= deadline:
            reasons.append("Контроль отменён или общий бюджет 180 с исчерпан; полного вывода нет.")
            break
        try:
            control.report(f"Контроль: серия {series + 1}/2, только зафиксированный вариант {selection.variant_number}…")
            trial = run_transaction_index_set_trial(workload.sql_text, settings, selection.copy_database,
                selection.actions, test_copy_confirmed=True, repetitions=3,
                minimum_gain_ms=selection.minimum_gain_ms, minimum_gain_ratio=selection.minimum_gain_ratio,
                deadline=deadline, control=control)
        except AnalysisCancelled:
            reasons.append("Контроль отменён до начала следующей пробы.")
            break
        trials.append(trial)
        if trial.status != "measured" or not trial.rollback_verified:
            reasons.append("Контроль не завершён: отказ/ошибка/отмена или неподтверждённый откат; другая гипотеза не проверяется.")
            break
    if control.cancelled.is_set():
        reasons.append("Контроль отменён; положительного вывода нет.")
    completed = len(trials) == 2 and not reasons
    if completed:
        problem = _baseline_problem(trials)
        if problem:
            reasons.append(problem)
        if not all(trial.accepted_for_reads for trial in trials):
            reasons.append("Не обе контрольные серии прошли условия пользы/регрессии/использования/дрейфа.")
    passed = completed and not reasons
    return TransactionHoldoutReport(selection, workload, tuple(trials), completed, passed, tuple(reasons))
