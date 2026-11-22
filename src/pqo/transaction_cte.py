"""Closed, statement-local SELECT CTE scopes; no execution or SQL inlining.

Catalog names, projected key lineage and index lookup lineage are separate.
Materialization preserves a key but may prevent an outer predicate from being
pushed to its base table. Neither fact is inferred from matching sample rows.
"""
from dataclasses import dataclass
import re

from sqlglot import exp, parse_one

from .transaction_defaults import identifier_key


_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')


@dataclass(frozen=True)
class CTEScope:
    definitions: tuple
    references: dict  # id(Table) -> CTE name; identifiers are folded like PostgreSQL


@dataclass(frozen=True)
class CTESource:
    columns: tuple[str, ...]
    keys: tuple[tuple[str, ...], ...]
    lineage: dict  # output column -> (schema, physical table, physical column) or None
    origin: tuple[str, str] | None = None
    pushdown: bool = False


def relation_key(table):
    return (table.db, table.name) if table.db else ('', identifier_key(table.this))


def cte_scope(tree):
    """Only a top-level WITH, SELECT bodies, earlier references and unique names."""
    clause = tree.args.get('with_')
    clauses = list(tree.find_all(exp.With))
    if not clause:
        if clauses:
            raise ValueError('CTE: WITH допускается только перед основной командой, без вложенных WITH.')
        return CTEScope((), {})
    if len(clauses) != 1 or type(clause) is not exp.With:
        raise ValueError('CTE: вложенные WITH не поддерживаются.')
    if any(value and key != 'expressions' for key, value in clause.args.items()):
        raise ValueError('CTE: RECURSIVE, SEARCH и CYCLE пока не поддерживаются.')
    definitions = tuple(clause.expressions)
    if not 1 <= len(definitions) <= 8:
        raise ValueError('CTE: допускается от 1 до 8 определений на команду.')
    if len(list(tree.find_all(exp.Table))) > 64:
        raise ValueError('CTE: не более 64 ссылок на источники во всей команде.')
    names, references = set(), {}
    target = tree.this if type(tree) in (exp.Insert, exp.Update, exp.Delete) else None
    target = target.this if type(target) is exp.Schema else target

    def bind(table, visible):
        if table.db or table.catalog:
            return
        if table is target:
            raise ValueError('CTE нельзя использовать как цель записи; укажите физическую схема.таблица.')
        name = identifier_key(table.this) if type(table.this) is exp.Identifier else None
        if name not in visible:
            raise ValueError('CTE: ссылка должна вести на предыдущее определение либо явную схема.таблица; самоссылки/ссылки вперёд исключены.')
        references[id(table)] = name

    for definition in definitions:
        alias = definition.args.get('alias')
        if (type(definition) is not exp.CTE or type(definition.this) is not exp.Select
                or type(alias) is not exp.TableAlias or type(alias.this) is not exp.Identifier
                or any(v and k not in {'this', 'alias', 'materialized'} for k, v in definition.args.items())
                or definition.args.get('materialized') not in (None, True, False)):
            raise ValueError('CTE: разрешены только SELECT, без команд записи, UNION и дополнительных параметров.')
        if any(v and k not in {'this', 'columns'} for k, v in alias.args.items()):
            raise ValueError('CTE: дополнительные параметры имени не поддерживаются.')
        name = identifier_key(alias.this)
        if name in names:
            raise ValueError('CTE: имена определений должны различаться.')
        for table in definition.this.find_all(exp.Table):
            bind(table, names)
        names.add(name)
    for table in tree.find_all(exp.Table):
        if table.find_ancestor(exp.CTE) is None:
            bind(table, names)
        if table is not target:
            alias = table.args.get('alias')
            if alias is not None and alias.args.get('columns'):
                raise ValueError('CTE: переименование столбцов источника через AS alias(...) не поддерживается; задайте имена в определении CTE.')
    return CTEScope(definitions, references)


def physical_tables(tree):
    scope = cte_scope(tree)
    return tuple(table for table in tree.find_all(exp.Table) if id(table) not in scope.references)


def _sources(select):
    source = select.args.get('from_')
    return ([source.this] if source else []) + [join.this for join in select.args.get('joins') or []]


def cte_catalog(tree, physical_columns, unique_keys=None):
    """Derive output names and only exact projected unique keys from catalogs.

    Single-source projections/filters preserve a full immediate key, even with
    LIMIT/DISTINCT/materialization. Joins and grouping do not receive an invented
    key. A global aggregate (or a SELECT without FROM) has at most one row.
    """
    scope = cte_scope(tree)
    if physical_columns is None:
        raise ValueError('CTE: для разрешения столбцов нужны каталоги физических источников.')
    unique_keys = unique_keys or {}
    catalog = {relation: CTESource(tuple(columns), tuple(unique_keys.get(relation, ())),
        {column: (*relation, column) for column in columns}, relation, True)
        for relation, columns in physical_columns.items()}
    counts = {}
    for name in scope.references.values():
        counts[name] = counts.get(name, 0) + 1
    for definition in scope.definitions:
        select = definition.this
        sources = _sources(select)
        if any(type(table) is not exp.Table for table in sources):
            raise ValueError('CTE: источники SELECT — прямые таблицы/предыдущие CTE, без FROM-подвыборок и LATERAL.')
        bindings = {}
        for table in sources:
            alias = table.args.get('alias')
            name = identifier_key(alias.this if alias is not None else table.this)
            relation = relation_key(table)
            if name in bindings or relation not in catalog:
                raise ValueError('CTE: неизвестный источник или повторный псевдоним.')
            bindings[name] = (table, catalog[relation])

        def resolve(column):
            name = identifier_key(column.this)
            if column.table:
                item = bindings.get(identifier_key(column.args['table']))
                if (item is None or column.catalog or column.db and
                        (item[0].args.get('alias') is not None or column.db != item[0].db)):
                    raise ValueError('CTE: неизвестный или скрытый псевдонимом источник столбца.')
                matches = [item]
            else:
                matches = [item for item in bindings.values() if name in item[1].columns]
            if len(matches) != 1 or name not in matches[0][1].columns:
                raise ValueError('CTE: неизвестный/неоднозначный выходной столбец; укажите источник.')
            return matches[0][1], name

        outputs, direct = [], []
        for projection in select.expressions:
            expression = projection.this if type(projection) is exp.Alias else projection
            if type(expression) is exp.Star or type(expression) is exp.Column and type(expression.this) is exp.Star:
                if type(projection) is exp.Alias:
                    raise ValueError('CTE: звёздочке нельзя присвоить одно имя столбца.')
                items = list(bindings.values())
                if type(expression) is exp.Column:
                    item = bindings.get(identifier_key(expression.args['table'])) if expression.table else None
                    if (item is None or expression.catalog or expression.db and
                            (item[0].args.get('alias') is not None or expression.db != item[0].db)):
                        raise ValueError('CTE: звёздочка относится к неизвестному источнику.')
                    items = [item]
                if not items:
                    raise ValueError('CTE: для звёздочки нужны известные источники.')
                for _, info in items:
                    for column in info.columns:
                        outputs.append(column)
                        direct.append((info, column))
            else:
                name = (identifier_key(projection.args['alias']) if type(projection) is exp.Alias
                    else identifier_key(expression.this) if type(expression) is exp.Column else None)
                outputs.append(name)
                direct.append(resolve(expression) if type(expression) is exp.Column else None)
        declared = definition.args['alias'].args.get('columns') or []
        if declared:
            if len(declared) != len(outputs) or any(type(x) is not exp.Identifier for x in declared):
                raise ValueError('CTE: список имён столбцов должен соответствовать всей выходной проекции.')
            outputs = [identifier_key(x) for x in declared]
        if not outputs or any(name is None or not _NAME.fullmatch(name) for name in outputs) or len(set(outputs)) != len(outputs):
            raise ValueError('CTE: выходные имена должны быть простыми и различными; вычислениям задайте AS имя.')
        lineage = {name: item[0].lineage[item[1]] if item else None for name, item in zip(outputs, direct)}
        has_aggregate = select.find(exp.AggFunc) is not None
        single = len(bindings) == 1 and not select.args.get('joins') and not select.args.get('group') and not has_aggregate
        keys = []
        if not sources or has_aggregate and not select.args.get('group'):
            keys = [()]
        elif single:
            info = next(iter(bindings.values()))[1]
            projected = {item[1]: name for name, item in zip(outputs, direct) if item and item[0] is info}
            keys = [tuple(projected[column] for column in key) for key in info.keys if set(key) <= projected.keys()]
        name = identifier_key(definition.args['alias'].this)
        foldable = definition.args.get('materialized') is False or counts.get(name, 0) == 1 and definition.args.get('materialized') is None
        parent = next(iter(bindings.values()))[1] if single else None
        pushdown = bool(parent and parent.pushdown and foldable and not select.args.get('limit') and not select.args.get('distinct'))
        catalog['', name] = CTESource(tuple(outputs), tuple(keys), lineage,
            parent.origin if pushdown else None, pushdown)
    return catalog


def validate_program_ctes(program, physical_columns):
    for command in program.commands:
        if command.kind in {'select', 'insert', 'update', 'delete'}:
            tree = parse_one(command.sql, read='postgres')
            if tree.args.get('with_'):
                cte_catalog(tree, physical_columns)
