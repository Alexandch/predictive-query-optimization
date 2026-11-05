"""Estimated statement plans, never transaction replay or EXPLAIN ANALYZE."""
from __future__ import annotations

from dataclasses import dataclass
import time

import psycopg
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import Scope, traverse_scope

from .analysis_control import AnalysisControl
from .config import DatabaseSettings
from .transaction_inspection import TransactionInspection, TransactionStatement, inspect_transaction


@dataclass(frozen=True)
class StatementPlan:
    number: int
    status: str  # planned, blocked, error, budget_exhausted
    explanation: str
    plan_json: list | None = None

    @property
    def root(self) -> dict:
        return self.plan_json[0]["Plan"] if self.plan_json else {}


@dataclass(frozen=True)
class TransactionPlanReport:
    inspection: TransactionInspection
    database_name: str
    statements: tuple[StatementPlan, ...]


def planning_restriction(statement: TransactionStatement, settings: DatabaseSettings) -> str | None:
    """Conservative preflight, not a security sandbox for a hostile database."""
    node = parse_one(statement.sql, read="postgres")
    if not statement.supported_structure:
        return "Этот тип команды пока не поддерживается для получения плана."
    if node.find(exp.Into) is not None or node.find(exp.Lock) is not None:
        return "SELECT INTO и явные блокировки строк пока не поддерживаются."
    if node.find(exp.Parameter, exp.Placeholder) is not None:
        return "Подставьте конкретные значения параметров вместо $1, :name или ?."
    if node.find(exp.Anonymous) is not None:
        return "Планирование неизвестных функций отключено: их побочные эффекты не установлены."
    forbidden = (exp.Command, exp.Create, exp.Drop, exp.Alter, exp.Copy, exp.Merge,
                 exp.Transaction, exp.Commit, exp.Rollback)
    if any(isinstance(item, forbidden) for item in node.walk()):
        return "Вложенная команда не поддерживается для получения плана."
    # A CTE reference is not a physical relation. Resolve it by lexical scope,
    # not a global set of aliases (which would hide unrelated unqualified tables).
    cte_references = set()
    try:
        for scope in traverse_scope(node) or ():
            for source_node, source in scope.selected_sources.values():
                if isinstance(source_node, exp.Table) and isinstance(source, Scope):
                    cte_references.add(id(source_node))
    except Exception:
        return "Не удалось однозначно разрешить области видимости таблиц и CTE."
    for table in node.find_all(exp.Table):
        if id(table) in cte_references:
            continue
        if table.catalog or not table.db or not isinstance(table.this, exp.Identifier):
            return "Используйте явные имена схема.таблица для всех физических таблиц."
        if table.db not in settings.allowed_schemas:
            return f"Схема {table.db} не входит в разрешённые схемы подключения."
    return None


def collect_transaction_plans(
    sql_text: str,
    settings: DatabaseSettings,
    *,
    control: AnalysisControl | None = None,
) -> TransactionPlanReport:
    """EXPLAIN each eligible command against the same initial snapshot.

    No supplied BEGIN/COMMIT/DML is executed. Commands do not see earlier
    commands' writes. Planner cost is not elapsed time. Even plain EXPLAIN
    takes locks and can invoke database-defined planner functions; use a test
    copy for untrusted schemas/functions. Read-only is a defence, not a sandbox.
    """
    control = control or AnalysisControl()
    control.check()
    inspection = inspect_transaction(sql_text)
    restrictions = [planning_restriction(item, settings) for item in inspection.statements]
    if all(restrictions):
        return TransactionPlanReport(inspection, settings.dbname, tuple(
            StatementPlan(item.number, "blocked", reason)
            for item, reason in zip(inspection.statements, restrictions)
        ))
    results = []
    deadline = time.monotonic() + 90
    with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
        with control.monitor(connection), connection.transaction(force_rollback=True):
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL search_path = pg_catalog")
            connection.execute("SET LOCAL standard_conforming_strings = on")
            connection.execute("SET LOCAL row_security = on")
            for item, reason in zip(inspection.statements, restrictions):
                control.check()
                if reason:
                    results.append(StatementPlan(item.number, "blocked", reason))
                    continue
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    results.append(StatementPlan(item.number, "budget_exhausted",
                        "Исчерпан общий бюджет получения планов (90 с)."))
                    continue
                timeout_ms = min(max(1, settings.statement_timeout_ms), 30_000, remaining_ms)
                control.report(f"Оценочный план команды {item.number}/{len(inspection.statements)}; без выполнения…")
                try:
                    # Roll back each savepoint to release its locks. Recover from
                    # one timeout/error without aborting the remaining inventory.
                    with connection.transaction(force_rollback=True):
                        connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
                        connection.execute("SELECT set_config('lock_timeout', %s, true)", (str(min(1000, timeout_ms)),))
                        plan = connection.execute("EXPLAIN (FORMAT JSON) " + item.sql).fetchone()[0]
                        if not isinstance(plan, list) or not plan or "Plan" not in plan[0]:
                            raise ValueError("PostgreSQL returned an invalid estimated plan")
                    results.append(StatementPlan(item.number, "planned", "Оценочный план; команда не выполнялась.", plan))
                except psycopg.Error as exc:
                    control.check()
                    if exc.sqlstate == "57014":
                        message = "Получение плана отменено сервером или превышен statement_timeout."
                    elif exc.sqlstate == "55P03":
                        message = "Не удалось получить блокировку за отведённое время."
                    else:
                        message = f"PostgreSQL [{exc.sqlstate or 'error'}]: {exc.diag.message_primary or type(exc).__name__}"
                    results.append(StatementPlan(item.number, "error", message))
    control.check()
    return TransactionPlanReport(inspection, settings.dbname, tuple(results))
