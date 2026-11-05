"""Rollback-only index experiment for the SELECT slice of a transaction, on a copy."""
from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median
import argparse
import math
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlglot import exp, parse_one

from .analysis_control import AnalysisCancelled, AnalysisControl
from .config import DatabaseSettings
from .explain import _assert_read_only_query
from .index_actions import IndexAction, IndexActionKind
from .transaction_indexes import _load_relation_indexes, read_statements_for_index
from .transaction_inspection import inspect_transaction
from .transaction_planning import planning_restriction


LAB_DATABASE = "pqo_university_index_lab"


@dataclass(frozen=True)
class TrialReading:
    number: int
    before_ms: tuple[float, ...]
    indexed_ms: tuple[float, ...]
    restored_ms: tuple[float, ...]
    used_trial_index: bool

    @property
    def baseline_ms(self):
        return float(median((*self.before_ms, *self.restored_ms)))

    @property
    def candidate_ms(self):
        return float(median(self.indexed_ms))


@dataclass(frozen=True)
class TransactionIndexTrial:
    action: IndexAction
    copy_database: str
    status: str  # measured, declined, error, cancelled
    readings: tuple[TrialReading, ...]
    skipped_writes: tuple[int, ...]
    reasons: tuple[str, ...]
    accepted_for_reads: bool
    rollback_verified: bool
    index_size_bytes: int = 0
    build_time_ms: float = 0
    minimum_gain_ms: float = 1.
    minimum_gain_ratio: float = .05
    repetitions: int = 3
    index_name: str = ""
    lookup_reads: tuple[int, ...] = ()
    additional_actions: tuple[IndexAction, ...] = ()
    index_names: tuple[str, ...] = ()
    used_index_names: tuple[str, ...] = ()
    frequencies: tuple[tuple[int, int], ...] = ()

    @property
    def actions(self):
        return (self.action, *self.additional_actions)

    @property
    def baseline_sum_ms(self):
        return sum(item.baseline_ms for item in self.readings)

    @property
    def indexed_sum_ms(self):
        return sum(item.candidate_ms for item in self.readings)

    @property
    def weighted_baseline_ms(self):
        weights = dict(self.frequencies)
        return sum(weights.get(item.number, 1) * item.baseline_ms for item in self.readings)

    @property
    def weighted_indexed_ms(self):
        weights = dict(self.frequencies)
        return sum(weights.get(item.number, 1) * item.candidate_ms for item in self.readings)

    @property
    def weighted_gain_ms(self):
        return self.weighted_baseline_ms - self.weighted_indexed_ms


def validate_frequencies(numbers, frequencies=None):
    """Explicit positive counts, not measured production rates or SQL replays."""
    numbers = set(numbers)
    weights = {number: 1 for number in numbers} if frequencies is None else dict(frequencies)
    if (set(weights) != numbers or any(type(number) is not int for number in weights)
            or any(type(value) is not int or not 1 <= value <= 10000 for value in weights.values())):
        raise ValueError("Укажите целые числа повторений 1–10000 для каждого самостоятельного SELECT, без DML/лишних номеров.")
    return tuple(sorted(weights.items()))


def assess_readings(readings, *, minimum_gain_ms, minimum_gain_ratio,
                    maximum_regression_ms=2., maximum_regression_ratio=.10, frequencies=None):
    """Frequency-weighted objective; per-SELECT regression/drift stays unweighted."""
    reasons = []
    if not readings:
        return ("Нет полного набора замеров SELECT.",)
    if not any(item.used_trial_index for item in readings):
        reasons.append("PostgreSQL не использовал пробный индекс ни в одном измеренном SELECT.")
    for item in readings:
        before, restored = median(item.before_ms), median(item.restored_ms)
        if abs(restored - before) > 2 and abs(restored - before) / max(min(before, restored), .001) > .20:
            reasons.append(f"SELECT {item.number}: исходное время дрейфует между A1 и A2; ускорение не подтверждено.")
        regression = item.candidate_ms - item.baseline_ms
        if regression > maximum_regression_ms and regression / max(item.baseline_ms, .001) > maximum_regression_ratio:
            reasons.append(f"SELECT {item.number}: существенная регрессия {item.baseline_ms:.2f} → {item.candidate_ms:.2f} мс.")
    weights = dict(validate_frequencies((item.number for item in readings), frequencies))
    baseline = sum(weights[item.number] * item.baseline_ms for item in readings)
    indexed = sum(weights[item.number] * item.candidate_ms for item in readings)
    gain = baseline - indexed
    if gain <= 0 or gain < minimum_gain_ms or gain / max(baseline, .001) < minimum_gain_ratio:
        reasons.append("Сумма медиан SELECT с заданными числами повторений не достигла обоих порогов выигрыша.")
    return tuple(reasons)


def _read_slice(sql_text, settings, action):
    inspection = inspect_transaction(sql_text)
    if inspection.unsupported_commands:
        raise ValueError("В транзакции есть неподдержанные команды контекста; фактическая проба недоступна.")
    queries, writes = [], []
    target_read = False
    for item in inspection.statements:
        tree = parse_one(item.sql, read="postgres")
        if item.changes_data:
            writes.append(item.number)
            continue
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ValueError(f"Команда {item.number}: разрешены только SELECT и пропускаемые команды записи.")
        restriction = planning_restriction(item, settings)
        if restriction:
            raise ValueError(f"SELECT {item.number}: {restriction}")
        query = _assert_read_only_query(item.sql)
        queries.append((item.number, query))
        target_read |= any(table.db == action.schema_name and table.name == action.table_name
                           for table in tree.find_all(exp.Table))
    if not 1 <= len(queries) <= 20 or not target_read:
        raise ValueError("Нужны 1–20 допустимых SELECT, хотя бы один читает таблицу кандидата. DML не измеряется.")
    return queries, tuple(writes)


def _lookup_reads_on_copy(connection, queries, settings, action, relation, deadline, control):
    """Rebind columns against copy catalogs before any execution or index DDL."""
    relations = {(table.db, table.name) for _, query in queries
        for table in parse_one(query, read="postgres").find_all(exp.Table) if table.db}
    if len(relations) > 64:
        raise ValueError("Для проверки применимости индекса допускается не более 64 таблиц SELECT.")
    metadata = {(action.schema_name, action.table_name): relation}
    for schema, table in sorted(relations - metadata.keys()):
        control.check()
        _timeout(connection, settings, deadline)
        other = _load_relation_indexes(connection, schema, table)
        if other is not None:
            metadata[schema, table] = other
    inspection = inspect_transaction("BEGIN; " + "; ".join(query for _, query in queries) + "; ROLLBACK;")
    selected = set(read_statements_for_index(inspection, action, metadata))
    return tuple(number for position, (number, _) in enumerate(queries, 1) if position in selected)


def _has_index(node, name):
    if isinstance(node, dict):
        return node.get("Index Name") == name or any(_has_index(value, name) for value in node.values())
    if isinstance(node, list):
        return any(_has_index(value, name) for value in node)
    return False


def _timeout(connection, settings, deadline):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("Общий бюджет пробы исчерпан.")
    connection.execute("SELECT set_config('statement_timeout', %s, true)",
        (str(min(30_000, max(1, settings.statement_timeout_ms), remaining)),))


def _measure_phase(connection, queries, repetitions, settings, deadline, control, label, index_name, *, index_usage=None):
    samples = {number: [] for number, _ in queries}
    used = set()
    names = (index_name,) if isinstance(index_name, str) else tuple(index_name)
    # READ ONLY is local to this rollback-only savepoint; rolling it back
    # restores the parent's RW mode needed for our own CREATE INDEX.
    with connection.transaction(force_rollback=True):
        connection.execute("SET LOCAL transaction_read_only = on")
        for run in range(repetitions + 1):
            for number, query in queries:
                control.report(f"{label}: SELECT {number}, {'прогрев' if run == 0 else f'замер {run}/{repetitions}'}…")
                _timeout(connection, settings, deadline)
                plan = connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query).fetchone()[0]
                value = plan[0].get("Execution Time")
                if value is None or not math.isfinite(float(value)) or value < 0:
                    raise ValueError("PostgreSQL не вернул корректное Execution Time.")
                if run:
                    samples[number].append(float(value))
                    matching = {name for name in names if _has_index(plan, name)}
                    if matching:
                        used.add(number)
                    if index_usage is not None:
                        index_usage.update(matching)
    return samples, used


def _verify_absent(settings, action, name):
    try:
        with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
            with connection.transaction(force_rollback=True):
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '2s'")
                return bool(connection.execute("""SELECT NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s)""", (action.schema_name, name)).fetchone()[0])
    except psycopg.Error:
        return False


def run_transaction_index_trial(sql_text, source_settings: DatabaseSettings, copy_database: str,
        action: IndexAction, *, test_copy_confirmed=False, repetitions=3,
        minimum_gain_ms=1., minimum_gain_ratio=.05, control=None):
    return _run_transaction_index_trial(sql_text, source_settings, copy_database, (action,),
        test_copy_confirmed=test_copy_confirmed, repetitions=repetitions,
        minimum_gain_ms=minimum_gain_ms, minimum_gain_ratio=minimum_gain_ratio, control=control)


def run_transaction_index_set_trial(sql_text, source_settings, copy_database, actions, *,
        test_copy_confirmed=False, repetitions=3, minimum_gain_ms=1., minimum_gain_ratio=.05,
        frequencies=None, deadline=None, control=None):
    """One or two temporary indexes together. Never pass this report off as a single-index trial."""
    return _run_transaction_index_trial(sql_text, source_settings, copy_database, tuple(actions),
        test_copy_confirmed=test_copy_confirmed, repetitions=repetitions,
        minimum_gain_ms=minimum_gain_ms, minimum_gain_ratio=minimum_gain_ratio,
        frequencies=frequencies, deadline=deadline, control=control)


def _run_transaction_index_trial(sql_text, source_settings, copy_database, actions, *,
        test_copy_confirmed=False, repetitions=3, minimum_gain_ms=1., minimum_gain_ratio=.05,
        frequencies=None, deadline=None, control=None):
    """Only connect to a differently named, explicitly confirmed test database.

    No source commands are replayed. DML and modifying CTEs are skipped. Three
    warm-cache phases A/B/A run SELECTs at initial state, with rollback-only DDL.
    Database functions/extensions can have external effects: this is not a
    sandbox and must not be used on production or untrusted database code.
    """
    control = control or AnalysisControl()
    control.check()
    copy_database = copy_database.strip()
    if not test_copy_confirmed or not copy_database or copy_database.casefold() == source_settings.dbname.casefold():
        raise ValueError("Укажите другую БД и явно подтвердите, что это отдельная тестовая копия.")
    if (not 1 <= len(actions) <= 2 or len(set(actions)) != len(actions)
            or any(action.kind is not IndexActionKind.CREATE or action.schema_name not in source_settings.allowed_schemas for action in actions)):
        raise ValueError("Недопустимый индексный кандидат/схема.")
    if not 1 <= repetitions <= 5 or not math.isfinite(minimum_gain_ms) or minimum_gain_ms < 0 or not 0 <= minimum_gain_ratio <= 1:
        raise ValueError("Некорректные пороги или число замеров.")
    action = actions[0]
    queries, writes = _read_slice(sql_text, source_settings, action)
    for extra in actions[1:]:
        _read_slice(sql_text, source_settings, extra)
    weights = validate_frequencies((number for number, _ in queries), frequencies)
    settings = replace(source_settings, dbname=copy_database)
    names = tuple("pqo_tx_trial_" + uuid4().hex[:16] for _ in actions)
    name = names[0]
    readings, reasons = (), ()
    status, accepted, attempted = "declined", False, []
    size, build_ms = 0, 0.
    lookup_reads, used_names = set(), set()
    deadline = min(time.monotonic() + 120, deadline) if deadline is not None else time.monotonic() + 120
    try:
        with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
            with control.monitor(connection), connection.transaction(force_rollback=True):
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET LOCAL search_path = pg_catalog")
                connection.execute("SET LOCAL standard_conforming_strings = on")
                connection.execute("SET LOCAL lock_timeout = '1s'")
                _timeout(connection, settings, deadline)
                if connection.execute("SELECT current_database()").fetchone()[0] != copy_database:
                    raise ValueError("Подключение не соответствует имени тестовой копии.")
                if not connection.execute("SELECT pg_try_advisory_xact_lock(hashtext('pqo-transaction-index-trial'))").fetchone()[0]:
                    raise ValueError("На этой копии уже выполняется другая индексная проба.")
                relations = {}
                for current in actions:
                    control.check()
                    _timeout(connection, settings, deadline)
                    kind = connection.execute("""SELECT c.relkind FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s""",
                        (current.schema_name, current.table_name)).fetchone()
                    if kind is None or kind[0] != "r":
                        raise ValueError("Проба пока поддерживает только обычную таблицу в копии БД.")
                    key = (current.schema_name, current.table_name)
                    if key not in relations:
                        relations[key] = _load_relation_indexes(connection, *key)
                    relation = relations[key]
                    if relation is None or not set((*current.key_columns, *current.include_columns)) <= set(relation.columns):
                        raise ValueError("Таблица/столбцы кандидата отсутствуют в копии.")
                    covered = any(index.compatible and index.keys[:len(current.key_columns)] == current.key_columns
                        and set(current.include_columns) <= set((*index.keys, *index.includes)) for index in relation.indexes)
                    relevant = _lookup_reads_on_copy(connection, queries, settings, current, relation, deadline, control)
                    if not relevant:
                        reasons = ("У кандидата нет структурного применения в самостоятельных SELECT. "
                            "Применение только в DML или простое чтение таблицы/столбца не являются пробой ускорения SELECT. "
                            "SELECT и CREATE INDEX не запускались; польза для записи не измерялась.",)
                        break
                    lookup_reads.update(relevant)
                    if covered:
                        reasons = ("В копии уже есть подходящий индекс; новый дубликат не создавался.",)
                        break
                if not reasons:
                    before, _ = _measure_phase(connection, queries, repetitions, settings, deadline, control, "A1 без индекса", name)
                    with connection.transaction(force_rollback=True):
                        for current, current_name in zip(actions, names):
                            control.report("Создание пробного индекса только на тестовой копии…")
                            _timeout(connection, settings, deadline)
                            ddl = sql.SQL("CREATE INDEX {} ON {}.{} ({})").format(sql.Identifier(current_name),
                                sql.Identifier(current.schema_name), sql.Identifier(current.table_name),
                                sql.SQL(", ").join(map(sql.Identifier, current.key_columns)))
                            if current.include_columns:
                                ddl += sql.SQL(" INCLUDE ({})").format(sql.SQL(", ").join(map(sql.Identifier, current.include_columns)))
                            attempted.append((current, current_name))
                            started = time.monotonic()
                            connection.execute(ddl)
                            build_ms += (time.monotonic() - started) * 1000
                            size += int(connection.execute("SELECT pg_catalog.pg_relation_size(%s::regclass)",
                                (f'"{current.schema_name}"."{current_name}"',)).fetchone()[0])
                            if size > 64 * 1024 * 1024:
                                raise ValueError("Пробные индексы превысили общий бюджет 64 МиБ; проверка остановлена.")
                        if len(names) == 1:
                            indexed, used = _measure_phase(connection, queries, repetitions, settings, deadline, control, "B с индексом", name)
                            if used:
                                used_names.add(name)
                        else:
                            indexed, used = _measure_phase(connection, queries, repetitions, settings, deadline, control,
                                "B с индексами", names, index_usage=used_names)
                    restored, _ = _measure_phase(connection, queries, repetitions, settings, deadline, control, "A2 после отката", name)
                    readings = tuple(TrialReading(number, tuple(before[number]), tuple(indexed[number]),
                        tuple(restored[number]), number in used) for number, _ in queries)
                    reasons = assess_readings(readings, minimum_gain_ms=minimum_gain_ms, minimum_gain_ratio=minimum_gain_ratio, frequencies=weights)
                    if len(names) > 1 and set(names) - used_names:
                        reasons += ("Не каждый индекс сочетания использован в измеренных планах; лишний индекс не обоснован.",)
                    status, accepted = "measured", not reasons
    except AnalysisCancelled:
        status, reasons = "cancelled", ("Проба отменена; полный вывод об ускорении не сделан.",)
    except (psycopg.Error, TimeoutError, ValueError) as exc:
        status = "error"
        if isinstance(exc, psycopg.Error) and exc.sqlstate == "57014":
            reasons = ("Проба остановлена сервером/statement_timeout; полного вывода об ускорении нет.",)
        elif isinstance(exc, psycopg.Error) and exc.sqlstate == "55P03":
            reasons = ("Превышен lock_timeout на тестовой копии; полного вывода об ускорении нет.",)
        else:
            reasons = (f"Проба не завершена: {type(exc).__name__}: {exc}",)
    # Evaluate every independent check, even if an earlier one failed.
    absent = [_verify_absent(settings, current, current_name) for current, current_name in attempted]
    rollback_verified = all(absent)
    if not rollback_verified:
        accepted = False
        reasons += tuple(f"Отсутствие пробного индекса {current.schema_name}.{current_name} после отката не подтверждено; проверьте копию."
            for (current, current_name), verified in zip(attempted, absent) if not verified)
    return TransactionIndexTrial(action, copy_database, status, readings, writes, reasons,
        accepted, rollback_verified, size, build_ms, minimum_gain_ms, minimum_gain_ratio, repetitions, name,
        tuple(sorted(lookup_reads)), actions[1:], names, tuple(sorted(used_names)), weights)


def create_university_lab(settings: DatabaseSettings):
    """Create one named disposable lab, never overwrite or terminate sessions."""
    with psycopg.connect(**replace(settings, dbname="postgres").connection_kwargs(), autocommit=True) as connection:
        connection.execute("SET statement_timeout = '15s'")
        if connection.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s", (LAB_DATABASE,)).fetchone():
            raise FileExistsError(f"{LAB_DATABASE} уже существует; ничего не перезаписано.")
        if not connection.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s", ("pqo_university_unseen",)).fetchone():
            raise ValueError("Нет учебной исходной БД pqo_university_unseen.")
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(LAB_DATABASE), sql.Identifier("pqo_university_unseen")))
    return LAB_DATABASE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create-university-lab",))
    parser.parse_args()
    print("Created", create_university_lab(DatabaseSettings.from_env()))


if __name__ == "__main__":
    main()
