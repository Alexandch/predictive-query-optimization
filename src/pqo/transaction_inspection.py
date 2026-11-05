"""Offline transaction inventory. This module never connects to or executes SQL."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path

from sqlglot import exp, parse
from sqlglot.errors import ParseError


@dataclass(frozen=True)
class TransactionStatement:
    number: int
    kind: str
    sql: str
    relation_names: tuple[str, ...]
    changes_data: bool
    supported_structure: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TransactionInspection:
    statements: tuple[TransactionStatement, ...]
    ending: str
    requires_test_copy: bool
    unsupported_commands: tuple[int, ...]
    execution_permitted: bool = False


def inspect_transaction(sql_text: str) -> TransactionInspection:
    """Understand one bare BEGIN ... COMMIT/ROLLBACK, without authorizing replay.

    Isolation clauses, savepoints, prepared transactions and session commands
    deliberately remain outside this first inventory stage.
    """
    if not sql_text.strip() or len(sql_text) > 500_000:
        raise ValueError("Transaction text must contain 1 to 500000 characters")
    try:
        # SQLGlot can represent comment-only trivia between commands as a
        # Semicolon node. It has no executable content and is not a command.
        nodes = [node for node in parse(sql_text, read="postgres")
                 if node is not None and not isinstance(node, exp.Semicolon)]
    except ParseError as exc:
        raise ValueError("Cannot parse transaction; no SQL was executed") from exc
    if len(nodes) < 3 or not isinstance(nodes[0], exp.Transaction):
        raise ValueError("Expected BEGIN, one or more commands, and COMMIT or ROLLBACK")
    if (nodes[0].args.get("modes") or nodes[0].args.get("this")
            or not isinstance(nodes[-1], (exp.Commit, exp.Rollback))
            or any(value is not None for value in nodes[-1].args.values())):
        raise ValueError("Only bare BEGIN and final COMMIT/ROLLBACK are supported at this stage")
    body = nodes[1:-1]
    if len(body) > 100:
        raise ValueError("At most 100 transaction commands can be inspected")
    if any(isinstance(node, (exp.Transaction, exp.Commit, exp.Rollback)) for node in body):
        raise ValueError("Only one complete transaction can be inspected")
    statements = []
    requires_copy = False
    for number, node in enumerate(body, 1):
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
        if any(not table.db for table in tables):
            warnings.append("Unqualified relation names require search_path resolution against the database")
        relation_names = set()
        for table in tables:
            relation = table.copy()
            relation.set("alias", None)
            relation_names.add(relation.sql(dialect="postgres"))
        relations = tuple(sorted(relation_names))
        statements.append(TransactionStatement(number, type(node).__name__.lower(),
            node.sql(dialect="postgres", pretty=True), relations, writes, supported, tuple(warnings)))
        requires_copy |= writes or locks or unknown_function or not supported
    return TransactionInspection(tuple(statements), type(nodes[-1]).__name__.lower(),
        requires_copy, tuple(item.number for item in statements if not item.supported_structure))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql_file", type=Path)
    args = parser.parse_args()
    result = inspect_transaction(args.sql_file.read_text(encoding="utf-8-sig"))
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
