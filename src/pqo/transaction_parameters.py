"""Offline, explicit, per-statement PostgreSQL positional value bindings.

Only AST value nodes are replaced. Identifiers, types and executable SQL are
never accepted as parameter values. Execution paths pin standard_conforming_strings
to on, as required by PostgreSQL literals generated here and by SQLGlot.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re
from uuid import UUID

from sqlglot import exp, parse_one

from .transaction_inspection import TransactionInspection, inspect_transaction


PARAMETER_TYPES = {
    "integer": "pg_catalog.int4", "bigint": "pg_catalog.int8",
    "numeric": "pg_catalog.numeric", "double precision": "pg_catalog.float8",
    "boolean": "pg_catalog.bool", "text": "pg_catalog.text",
    "date": "pg_catalog.date", "timestamp": "pg_catalog.timestamp",
    "timestamptz": "pg_catalog.timestamptz", "uuid": "pg_catalog.uuid",
}


@dataclass(frozen=True)
class ParameterSlot:
    statement_number: int
    position: int


@dataclass(frozen=True)
class ParameterValue:
    statement_number: int
    position: int
    type_name: str
    value: str = ""
    is_null: bool = False


@dataclass(frozen=True)
class BoundTransaction:
    source_sql: str
    sql_text: str
    parameters: tuple[ParameterValue, ...]
    inspection: TransactionInspection


def _position(parameter):
    literal = parameter.this
    if (not isinstance(literal, exp.Literal) or literal.is_string
            or not re.fullmatch(r"[1-9][0-9]*", str(literal.this)) or not 1 <= int(literal.this) <= 64):
        raise ValueError("Поддерживаются только позиционные параметры PostgreSQL $1–$64.")
    return int(literal.this)


def discover_transaction_parameters(sql_text):
    inspection = inspect_transaction(sql_text)
    slots = []
    for item in inspection.statements:
        tree = parse_one(item.sql, read="postgres")
        if tree.find(exp.Placeholder) is not None:
            raise ValueError("Пока поддерживаются только параметры $1, $2, …; :name и ? не поддерживаются.")
        positions = sorted({_position(node) for node in tree.find_all(exp.Parameter)})
        if positions and positions != list(range(1, max(positions) + 1)):
            raise ValueError(f"Команда {item.number}: номера параметров должны идти от $1 без пропусков.")
        slots.extend(ParameterSlot(item.number, position) for position in positions)
    if len(slots) > 128:
        raise ValueError("Допускается не более 128 параметров суммарно по командам.")
    return tuple(slots)


def _value_node(binding):
    if binding.type_name not in PARAMETER_TYPES or type(binding.is_null) is not bool:
        raise ValueError("Выберите один из поддерживаемых встроенных типов и явный признак NULL.")
    value = binding.value
    if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
        raise ValueError("Значение должно содержать не более 4000 символов без NUL.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Значение содержит недопустимую Unicode-последовательность.") from exc
    if binding.is_null:
        if value:
            raise ValueError("Для NULL поле значения должно быть пустым.")
        literal = exp.Null()
    else:
        raw = value.strip()
        kind = binding.type_name
        if kind in {"integer", "bigint"}:
            if not re.fullmatch(r"[+-]?[0-9]+", raw):
                raise ValueError("Ожидалось целое число.")
            number = int(raw)
            bits = 32 if kind == "integer" else 64
            if not -(2 ** (bits - 1)) <= number < 2 ** (bits - 1):
                raise ValueError("Целое число вне диапазона выбранного типа.")
            literal = exp.Literal.number(str(number))
        elif kind in {"numeric", "double precision"}:
            if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
                raise ValueError("Ожидалось конечное число с точкой, без разделителей разрядов.")
            try:
                number = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError("Некорректное число.") from exc
            if not number.is_finite() or len(number.as_tuple().digits) > 1000 or abs(number.as_tuple().exponent) > 1000:
                raise ValueError("Лимит числового значения: 1000 значащих цифр и модуль экспоненты 1000.")
            if kind == "double precision" and (not math.isfinite(float(number)) or number != 0 and float(number) == 0):
                raise ValueError("Число вне конечного диапазона double precision.")
            literal = exp.Literal.number(str(number))
        elif kind == "boolean":
            if raw not in {"true", "false"}:
                raise ValueError("Для boolean укажите true или false.")
            literal = exp.Boolean(this=raw == "true")
        elif kind == "text":
            literal = exp.Literal.string(value)
        elif kind == "date":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                raise ValueError("Дата должна иметь вид YYYY-MM-DD.")
            literal = exp.Literal.string(date.fromisoformat(raw).isoformat())
        elif kind in {"timestamp", "timestamptz"}:
            pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
            if kind == "timestamptz":
                pattern += r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
            if not re.fullmatch(pattern, raw):
                raise ValueError("Ожидалась ISO дата/время с секундами; для timestamptz обязателен UTC-сдвиг или Z, точность до 6 знаков.")
            stamp = datetime.fromisoformat(raw)
            if stamp.utcoffset() is not None and abs(stamp.utcoffset().total_seconds()) > 14 * 3600:
                raise ValueError("Поддерживается сдвиг часового пояса не более 14 часов.")
            literal = exp.Literal.string(stamp.isoformat(sep=" "))
        elif kind == "uuid":
            literal = exp.Literal.string(str(UUID(raw)))
    # Type is from a closed map, not user-supplied SQL. Always retain the cast:
    # a bound ORDER BY $1 must not become positional ORDER BY 1.
    cast = parse_one("CAST(NULL AS " + PARAMETER_TYPES[binding.type_name] + ")", read="postgres")
    cast.set("this", literal)
    return cast


def bind_transaction_parameters(sql_text, parameters):
    """Materialize one explicit value scenario, never a production prepared plan.

    Values stay in memory. No connections, guessing defaults, session commands,
    PREPARE, query replay or modifications to the original editor text.
    """
    slots = discover_transaction_parameters(sql_text)
    parameters = tuple(parameters)
    keys = [(item.statement_number, item.position) for item in parameters]
    if (any(type(item.statement_number) is not int or type(item.position) is not int for item in parameters)
            or len(set(keys)) != len(keys) or set(keys) != {(slot.statement_number, slot.position) for slot in slots}):
        raise ValueError("Задайте ровно по одному типу/значению для каждого параметра каждой команды, без лишних и повторных строк.")
    values = {}
    for binding in parameters:
        try:
            values[binding.statement_number, binding.position] = _value_node(binding)
        except (ValueError, InvalidOperation, OverflowError) as exc:
            # No raw value in diagnostic text: it may be sensitive.
            raise ValueError(f"Команда {binding.statement_number}, ${binding.position}: значение не соответствует выбранному типу или лимитам.") from exc
    inspection = inspect_transaction(sql_text)
    statements = []
    for item in inspection.statements:
        tree = parse_one(item.sql, read="postgres")
        for node in list(tree.find_all(exp.Parameter)):
            node.replace(values[item.number, _position(node)].copy())
        statements.append(tree.sql(dialect="postgres", pretty=True))
    bound = "BEGIN;\n\n" + ";\n\n".join(statements) + ";\n\n" + inspection.ending.upper() + ";"
    rebound = inspect_transaction(bound)
    signature = lambda report: tuple((item.kind, item.changes_data, item.supported_structure, item.relation_names) for item in report.statements)
    if signature(inspection) != signature(rebound) or discover_transaction_parameters(bound):
        raise ValueError("Параметры не могут менять структуру команд или имена таблиц; SQL не запускался.")
    return BoundTransaction(sql_text, bound, tuple(sorted(parameters, key=lambda item: (item.statement_number, item.position))), rebound)


def is_typed_constant(node):
    """Recognize only closed-map scalar CAST literals for index predicate binding."""
    if not isinstance(node, exp.Cast) or not isinstance(node.this, (exp.Literal, exp.Boolean, exp.Null, exp.Neg)):
        return False
    value = node.this
    if isinstance(value, exp.Neg) and not isinstance(value.this, exp.Literal):
        return False
    target = node.args.get("to")
    return target is not None and target.sql(dialect="postgres").lower() in set(PARAMETER_TYPES.values())
