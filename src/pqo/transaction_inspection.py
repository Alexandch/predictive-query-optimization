"""Offline transaction inventory. This module never connects to or executes SQL."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from .transaction_context import (TransactionOptions, parse_replay_call, parse_transaction_control, split_transaction_sql)


@dataclass(frozen=True)
class TransactionStatement:
    number: int
    kind: str
    sql: str
    relation_names: tuple[str, ...]
    changes_data: bool
    supported_structure: bool
    warnings: tuple[str, ...]
    options: TransactionOptions | None = None
    savepoint: str | None = None


@dataclass(frozen=True)
class TransactionInspection:
    statements: tuple[TransactionStatement, ...]
    ending: str
    requires_test_copy: bool
    unsupported_commands: tuple[int, ...]
    execution_permitted: bool = False
    options: TransactionOptions = TransactionOptions()

    @property
    def effective_options(self):
        options = self.options
        for statement in self.statements:
            if statement.kind == 'set_transaction':
                options = options.overlay(statement.options)
        return options


def render_transaction_statement(node, *, pretty=False):
    """Preserve PostgreSQL's DEFAULT VALUES before ON CONFLICT/RETURNING.

    SQLGlot's Insert generator puts the conflict before DEFAULT VALUES. Render
    those AST clauses separately; never search/replace user text or literals.
    """
    if isinstance(node,exp.Insert) and node.args.get('default') and node.args.get('conflict'):
        head=node.copy()
        for key in ('default','conflict','returning'):head.set(key,None)
        parts=[head.sql(dialect='postgres',pretty=pretty),'DEFAULT VALUES',
            node.args['conflict'].sql(dialect='postgres',pretty=pretty)]
        if node.args.get('returning'):parts.append(node.args['returning'].sql(dialect='postgres',pretty=pretty))
        return ('\n' if pretty else ' ').join(parts)
    return node.sql(dialect='postgres',pretty=pretty)


def inspect_transaction(sql_text: str) -> TransactionInspection:
    """Inventory transaction modes and controls without authorizing execution."""
    if not sql_text.strip() or len(sql_text) > 500_000:
        raise ValueError("Transaction text must contain 1 to 500000 characters")
    try:
        fragments = split_transaction_sql(sql_text)
        begin = parse_transaction_control(fragments[0]) if fragments else None
        ending = parse_transaction_control(fragments[-1]) if fragments else None
    except ParseError as exc:
        raise ValueError("Cannot parse transaction; no SQL was executed") from exc
    if len(fragments) < 3 or begin is None or begin.kind != 'begin':
        raise ValueError("Expected BEGIN, one or more commands, and COMMIT or ROLLBACK")
    if ending is None or ending.kind not in {'commit','rollback'}:
        raise ValueError('Нужен завершающий COMMIT или ROLLBACK без CHAIN.')
    body = fragments[1:-1]
    if len(body) > 100:
        raise ValueError("At most 100 transaction commands can be inspected")
    statements = []
    requires_copy = False
    context_open = True
    for number, fragment in enumerate(body, 1):
        command = parse_transaction_control(fragment)
        if command is not None:
            if command.kind in {'begin','commit','rollback'}:
                raise ValueError('Можно разобрать только одну полную транзакцию.')
            if command.kind == 'set_transaction' and not context_open:
                raise ValueError('SET TRANSACTION поддерживается только в начале, до запросов и SAVEPOINT.')
            if command.kind == 'set_role' and not context_open:
                raise ValueError('SET LOCAL ROLE допускается один раз в начале, после всех SET TRANSACTION.')
            context_open = context_open and command.kind == 'set_transaction'
            statements.append(TransactionStatement(number,command.kind,command.sql,(),False,True,(),command.options,command.savepoint))
            continue
        context_open = False
        if call := parse_replay_call(fragment):
            schema, name = call
            statements.append(TransactionStatement(number, 'call', f'CALL "{schema}"."{name}"()', (), True, True,
                ('Процедура подлежит проверке каталога на лабораторном эталоне.',)))
            requires_copy = True
            continue
        try:
            node = parse_one(fragment,read='postgres')
        except ParseError:
            raise ValueError('Cannot parse transaction; no SQL was executed') from None
        writes = any(isinstance(item, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Into))
                     for item in node.walk())
        locks = node.find(exp.Lock) is not None
        unknown_function = node.find(exp.Anonymous) is not None
        supported = isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except,
                                      exp.Insert, exp.Update, exp.Delete))
        warnings = []
        if writes:
            warnings.append("Data-changing command: execution requires an isolated test copy")
        if locks:
            warnings.append("Row locks requested; this is not an ordinary read-only query")
        if unknown_function:
            warnings.append("Function effects are unknown; SELECT alone does not establish safety")
        if node.find(exp.Parameter, exp.Placeholder) is not None:
            warnings.append("Unresolved parameters require explicit values before planning")
        if not supported:
            warnings.append("Command structure is not supported by the planning stage")
        aliases = {cte.alias_or_name for cte in node.find_all(exp.CTE)}
        tables = [table for table in node.find_all(exp.Table)
                  if table.db or table.name not in aliases]
        if node.args.get('with_'):
            from .transaction_cte import physical_tables
            try:
                tables = list(physical_tables(node))
            except ValueError:
                warnings.append('CTE execution requires supported SELECT-only scopes')
        if any(not table.db for table in tables):
            warnings.append("Unqualified relation names require search_path resolution against the database")
        relation_names = set()
        for table in tables:
            relation = table.copy()
            relation.set("alias", None)
            relation.set("joins", None)
            relation_names.add(relation.sql(dialect="postgres"))
        relations = tuple(sorted(relation_names))
        statements.append(TransactionStatement(number, type(node).__name__.lower(),
            render_transaction_statement(node,pretty=True), relations, writes, supported, tuple(warnings)))
        requires_copy |= writes or locks or unknown_function or not supported
    return TransactionInspection(tuple(statements), ending.kind,
        requires_copy, tuple(item.number for item in statements if not item.supported_structure),options=begin.options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql_file", type=Path)
    args = parser.parse_args()
    result = inspect_transaction(args.sql_file.read_text(encoding="utf-8-sig"))
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
