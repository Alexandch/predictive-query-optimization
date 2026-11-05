"""Offline row×new-index exposure scenarios, NOT write-time prediction or DML replay."""
from dataclasses import dataclass
import math

from sqlglot import exp, parse_one

from .analysis_control import AnalysisControl
from .index_actions import IndexActionKind
from .transaction_indexes import _writes
from .transaction_inspection import inspect_transaction


@dataclass(frozen=True)
class WriteAssumption:
    number: int
    repetitions: int = 1
    rows_per_call: int | None = None


@dataclass(frozen=True)
class WritePressureRow:
    number: int
    kind: str
    targets: tuple[tuple[str, str], ...]
    repetitions: int
    rows_per_call: int | None
    index_count: int
    exposure: int | None
    plan_input_rows: float | None
    key_columns_in_set: tuple[str, ...]
    include_columns_in_set: tuple[str, ...]
    incremental_hot_columns: tuple[str, ...] | None
    hot_status: str
    historical_hot_percent: float | None
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class TransactionWritePressure:
    actions: tuple
    rows: tuple[WritePressureRow, ...]
    known_exposure: int
    unknown_commands: tuple[int, ...]
    scope_complete: bool
    budget: int | None
    budget_state: str  # not_set, exceeded, within_scenario, unknown
    read_trial: object | None
    notes: tuple[str, ...]

    @property
    def net_benefit_ms(self):
        return None  # Deliberately no conversion of structural units to milliseconds.


def _plan_input_rows(plan, kind):
    if plan is None or plan.status != "planned":
        return None
    root = plan.root
    children = root.get("Plans", ())
    if root.get("Node Type") != "ModifyTable" or root.get("Operation", "").lower() != kind or len(children) != 1:
        return None
    rows = children[0].get("Plan Rows")
    return float(rows) if type(rows) in (int, float) and math.isfinite(rows) and rows >= 0 else None


def write_assumptions(inspection, assumptions=None):
    numbers = {item.number for item in inspection.statements if item.changes_data}
    values = tuple(WriteAssumption(number) for number in sorted(numbers)) if assumptions is None else tuple(assumptions)
    if (any(not isinstance(item, WriteAssumption) or type(item.number) is not int for item in values)
            or len({item.number for item in values}) != len(values) or {item.number for item in values} != numbers
            or any(type(item.repetitions) is not int or not 1 <= item.repetitions <= 10000
                or (item.rows_per_call is not None and (type(item.rows_per_call) is not int or not 0 <= item.rows_per_call <= 1_000_000_000)) for item in values)):
        raise ValueError("Для каждой команды записи задайте повторы 1–10000 и строки 0–1000000000 либо оставьте строки неизвестными; без лишних/повторных номеров.")
    return {item.number: item for item in values}


def assess_write_pressure(sql_text, plans, index_report, actions, *, assumptions=None, budget=None, read_trial=None, control=None):
    """Use explicit scenario counts only. Catalogs/plans were collected earlier.

    A unit means one assumed affected row exposed to one additional full B-tree.
    INSERT entry work, potential non-HOT UPDATE work and deferred DELETE cleanup
    are intentionally NOT equated to actual index operations, bytes, WAL or time.
    Unknown rows/side effects never silently become zero or a complete net win.
    """
    control = control or AnalysisControl()
    control.check()
    inspection = inspect_transaction(sql_text)
    if inspection != plans.inspection or plans.database_name != index_report.database_name:
        raise ValueError("Планы/кандидаты не соответствуют текущему SQL и базе; получите их заново.")
    actions = tuple(actions)
    candidates = {item.action: item for item in index_report.candidates}
    if (not 1 <= len(actions) <= 3 or len(set(actions)) != len(actions)
            or any(action.kind is not IndexActionKind.CREATE or action not in candidates
                or candidates[action].status == "covered" for action in actions)):
        raise ValueError("Нужны 1–3 разных непокрытых кандидата текущего анализа; существующий индекс повторно не считается новым.")
    if budget is not None and (type(budget) is not int or not 0 <= budget <= 1_000_000_000_000_000):
        raise ValueError("Сценарный лимит должен быть целым 0–1000000000000000 либо не задан.")
    assumptions = write_assumptions(inspection, assumptions)
    metadata = {(item.schema, item.table): item for item in index_report.relations}
    metadata.update({(item.relation.schema, item.relation.table): item.relation for item in index_report.candidates})
    selected = {}
    for action in actions:
        selected.setdefault((action.schema_name, action.table_name), []).append(action)
    trees = {item.number: parse_one(item.sql, read="postgres") for item in inspection.statements}
    writes = _writes(trees)
    plans_by_number = {plan.number: plan for plan in plans.statements}
    rows, unknown, notes = [], [], []
    complete = not inspection.unsupported_commands and not index_report.truncated
    for statement in inspection.statements:
        control.check()
        if not statement.changes_data:
            continue
        control.report(f"Локальная оценка записи: команда {statement.number}; SQL не выполняется…")
        tree = trees[statement.number]
        ops = [item for item in writes if item[0] == statement.number]
        targets = tuple(sorted({item[2] for item in ops}))
        relevant = [action for target in targets for action in selected.get(target, ())]
        count = len(relevant)
        simple = (isinstance(tree, (exp.Insert, exp.Update, exp.Delete)) and len(ops) == 1
            and not tree.args.get("conflict") and tree.find(exp.Into) is None)
        kind = type(tree).__name__.lower()
        assumption = assumptions[statement.number]
        limits = []
        plan = plans_by_number.get(statement.number)
        if plan is None or plan.status != "planned":
            complete = False
            limits.append("Нет допустимого оценочного плана команды записи; её серверная семантика не подтверждена.")
        if not simple:
            complete = False
            limits.append("Сложная запись/изменяющий CTE/MERGE/ON CONFLICT: ветви и затронутые строки не моделируются.")
        if tree.find(exp.Anonymous) is not None:
            complete = False
            limits.append("Есть функция с неизвестными побочными эффектами; дополнительные записи не исключены.")
        relation = metadata.get(targets[0]) if len(targets) == 1 else None
        if relation is None or not relation.write_metadata_known:
            complete = False
            limits.append("Метаданные записи отсутствуют/неполны: триггеры, правила, секции и каскады не исключены.")
        elif (relation.relation_kind != "r" or relation.has_descendants or relation.has_user_triggers or relation.has_rules
                or kind == "update" and relation.fk_update_writes or kind == "delete" and relation.fk_delete_writes):
            complete = False
            limits.append("Есть секции/наследование, пользовательские триггеры, правила или возможная каскадная запись; побочные операции вне модели.")
        exposure = None
        if simple:
            if count == 0:
                exposure = 0  # Only direct exposure; indirect writes remain explicitly incomplete.
            elif assumption.rows_per_call is not None:
                exposure = assumption.repetitions * assumption.rows_per_call * count
        if exposure is None:
            unknown.append(statement.number)
        columns = ops[0][3] if simple and kind == "update" else None
        if columns is not None and relation is not None and not columns <= set(relation.columns):
            complete = False
            limits.append("SET содержит неразрешённые по метаданным столбцы; модель неполная.")
        keys = tuple(sorted({name for action in relevant for name in action.key_columns if columns is not None and name in columns}))
        includes = tuple(sorted({name for action in relevant for name in action.include_columns if columns is not None and name in columns}))
        hot, incremental, historical = "not_update", (), None
        if kind == "update":
            dependencies_known = (relation is not None and relation.write_metadata_known and columns is not None
                and not relation.generated_columns and not relation.has_user_triggers and not relation.has_rules
                and relation.relation_kind == "r" and not relation.has_descendants
                and all(index.compatible and all(name is not None for name in (*index.keys, *index.includes)) for index in relation.indexes))
            if not dependencies_known:
                hot, incremental = "unknown", None
                limits.append("Зависимости HOT не установлены полностью (включая выражения/предикаты/генерируемые столбцы); вывод о сохранении HOT невозможен.")
            else:
                existing = {name for index in relation.indexes for name in (*index.keys, *index.includes)}
                incremental = tuple(sorted((set(keys) | set(includes)) - existing))
                hot = "new_columns_in_set" if incremental else "existing_columns_in_set" if columns & existing else "no_direct_overlap"
            if relation is not None and relation.updates > 0 and 0 <= relation.hot_updates <= relation.updates:
                historical = 100 * relation.hot_updates / relation.updates
            limits.append("SET — не доказательство изменения значения. Даже без пересечения HOT требует места на странице; при не-HOT новые записи могут понадобиться всем обычным индексам.")
        rows.append(WritePressureRow(statement.number, kind, targets, assumption.repetitions, assumption.rows_per_call,
            count, exposure, _plan_input_rows(plans_by_number.get(statement.number), kind) if simple else None,
            keys, includes, incremental, hot, historical, tuple(limits)))
    known = sum(row.exposure for row in rows if row.exposure is not None)
    state = "not_set" if budget is None else "exceeded" if known > budget else "unknown" if unknown or not complete else "within_scenario"
    if read_trial is not None:
        read_numbers = {item.number for item in inspection.statements if not item.changes_data}
        if (read_trial.actions != actions or read_trial.status != "measured" or not read_trial.rollback_verified
                or {reading.number for reading in read_trial.readings} != read_numbers):
            raise ValueError("Замер чтений относится к другому набору индексов/SELECT или не завершён с подтверждённым откатом.")
    if index_report.truncated:
        notes.append("Каталог кандидатов усечён; оценка неполная.")
    if inspection.unsupported_commands:
        notes.append("Есть неподдержанный контекст/команды; влияние всей транзакции не установлено.")
    notes.extend(("Единица — строка × новый индекс по явному допущению, не операция B-tree, WAL, байты или миллисекунды.",
        "INSERT, возможное обслуживание при UPDATE и будущая очистка DELETE имеют разную стоимость; общий счётчик — только структурная экспозиция.",
        "Плановый поток и накопленная доля HOT справочные; они не подставляются вместо заданных строк или вероятности будущего HOT.",
        "Чистый выигрыш транзакции не вычислен. Лимит пользовательский, не порог безопасности PostgreSQL. DML не выполнялся."))
    if not rows:
        notes.append("В тексте нет явных команд записи; внешняя нагрузка этим не исключается.")
    control.check()
    return TransactionWritePressure(actions, tuple(rows), known, tuple(unknown), complete, budget, state, read_trial, tuple(notes))
