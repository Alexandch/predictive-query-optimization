"""Safe, finite index actions for the recommendation agent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from sqlglot import exp, parse_one

from .explain import _assert_read_only_query


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class IndexActionKind(StrEnum):
    NOOP = "noop"
    CREATE = "create"


@dataclass(frozen=True, slots=True)
class IndexAction:
    """One allowlisted action; arbitrary DDL is deliberately impossible."""

    kind: IndexActionKind
    schema_name: str | None = None
    table_name: str | None = None
    key_columns: tuple[str, ...] = ()
    include_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is IndexActionKind.NOOP:
            if any((self.schema_name, self.table_name, self.key_columns, self.include_columns)):
                raise ValueError("NOOP action cannot contain index fields")
            return

        if not self.schema_name or not self.table_name or not self.key_columns:
            raise ValueError("CREATE action requires schema, table and key columns")
        if len(self.key_columns) > 3 or len(self.include_columns) > 3:
            raise ValueError("Index actions are limited to three key and include columns")
        if set(self.key_columns) & set(self.include_columns):
            raise ValueError("INCLUDE columns must not duplicate key columns")

        identifiers = (
            self.schema_name,
            self.table_name,
            *self.key_columns,
            *self.include_columns,
        )
        if any(not _IDENTIFIER.fullmatch(value) for value in identifiers):
            raise ValueError("Unsafe SQL identifier in index action")

    @classmethod
    def noop(cls) -> "IndexAction":
        return cls(IndexActionKind.NOOP)

    @classmethod
    def create(
        cls,
        schema_name: str,
        table_name: str,
        key_columns: tuple[str, ...],
        include_columns: tuple[str, ...] = (),
    ) -> "IndexAction":
        return cls(
            IndexActionKind.CREATE,
            schema_name,
            table_name,
            key_columns,
            include_columns,
        )


def generate_index_actions(
    sql_text: str,
    *,
    allowed_schemas: frozenset[str] = frozenset({"aviation"}),
    max_actions: int = 24,
) -> tuple[IndexAction, ...]:
    """Derive deterministic index candidates from predicates and ordering."""
    query = _assert_read_only_query(sql_text)
    tree = parse_one(query, read="postgres")

    alias_to_table: dict[str, tuple[str, str]] = {}
    for table in tree.find_all(exp.Table):
        schema_name = table.db or "public"
        if schema_name in allowed_schemas:
            alias_to_table[table.alias_or_name] = (schema_name, table.name)

    predicate_columns = _columns_by_table(
        _predicate_nodes(tree), alias_to_table
    )
    order_columns = _columns_by_table(tree.find_all(exp.Order), alias_to_table)
    selected_columns = _columns_by_table(
        (
            expression
            for select in tree.find_all(exp.Select)
            for expression in select.expressions
        ),
        alias_to_table,
    )
    select_aliases = {
        expression.alias
        for select in tree.find_all(exp.Select)
        for expression in select.expressions
        if expression.alias
    }
    order_columns = {
        alias: [column for column in columns if column not in select_aliases]
        for alias, columns in order_columns.items()
    }
    selected_columns = {
        alias: [column for column in columns if column not in select_aliases]
        for alias, columns in selected_columns.items()
    }

    actions: list[IndexAction] = [IndexAction.noop()]
    seen: set[IndexAction] = set(actions)
    for alias, (schema_name, table_name) in alias_to_table.items():
        predicates = predicate_columns.get(alias, [])
        ordering = order_columns.get(alias, [])
        includes = selected_columns.get(alias, [])

        key_candidates: list[tuple[str, ...]] = [(column,) for column in predicates]
        combined = _unique((*predicates, *ordering))[:3]
        if len(combined) > 1:
            key_candidates.append(tuple(combined))

        for keys in key_candidates:
            action = IndexAction.create(schema_name, table_name, keys)
            _append_unique(actions, seen, action, max_actions)

            covering = tuple(
                column for column in includes if column not in keys
            )[:3]
            if covering:
                action = IndexAction.create(schema_name, table_name, keys, covering)
                _append_unique(actions, seen, action, max_actions)

            if len(actions) >= max_actions:
                return tuple(actions)

    return tuple(actions)


def _predicate_nodes(tree: exp.Expression):
    yield from tree.find_all(exp.Where)
    for join in tree.find_all(exp.Join):
        condition = join.args.get("on")
        if condition is not None:
            yield condition


def _columns_by_table(
    nodes,
    alias_to_table: dict[str, tuple[str, str]],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    only_alias = next(iter(alias_to_table), None) if len(alias_to_table) == 1 else None
    for node in nodes:
        for column in node.find_all(exp.Column):
            alias = column.table or only_alias
            if alias not in alias_to_table:
                continue
            columns = result.setdefault(alias, [])
            if column.name not in columns:
                columns.append(column.name)
    return result


def _unique(values) -> list[str]:
    return list(dict.fromkeys(values))


def _append_unique(
    actions: list[IndexAction],
    seen: set[IndexAction],
    action: IndexAction,
    max_actions: int,
) -> None:
    if len(actions) < max_actions and action not in seen:
        actions.append(action)
        seen.add(action)
