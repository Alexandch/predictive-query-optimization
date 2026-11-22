"""Flat PostgreSQL joined writes and catalog-only cardinality proofs.

Never run a probe against source rows to guess uniqueness. A mandatory equality
must bind every column of an immediate full unique key before an UPDATE source
is admitted. DELETE may have many matches unless RETURNING reads a source row.
"""
from sqlglot import exp, parse_one

from .transaction_defaults import identifier_key, is_default
from .transaction_cte import cte_catalog, relation_key


def _terms(node):
    if type(node) is exp.Paren:
        yield from _terms(node.this)
    elif type(node) is exp.And:
        yield from _terms(node.this)
        yield from _terms(node.expression)
    elif node is not None:
        yield node


def _alias(table):
    alias = table.args.get('alias')
    return identifier_key(alias.this if alias is not None else table.this)


def joined_dml_sources(tree):
    """Validate the closed FROM/USING shape; return target then flat sources.

    SQLGlot attaches DML source joins to the first Table, unlike SELECT joins.
    Comma items are Join nodes without ON; their connection is checked from
    mandatory WHERE equalities after columns have been bound from catalogs.
    """
    if type(tree) not in (exp.Update, exp.Delete):
        return ()
    source = tree.args.get('from_') if type(tree) is exp.Update else tree.args.get('using')
    if not source:
        return ()
    if type(tree) is exp.Update:
        if type(source) is not exp.From or any(v and k != 'this' for k, v in source.args.items()):
            raise ValueError('UPDATE FROM: нужен прямой источник без дополнительных параметров.')
        bases = [source.this]
    else:
        bases = source if isinstance(source, list) else []
    if len(bases) != 1 or type(bases[0]) is not exp.Table or type(tree.this) is not exp.Table:
        raise ValueError('FROM/USING: поддерживаются только прямые таблицы, без подзапросов и LATERAL.')
    joins = bases[0].args.get('joins') or []
    tables = (tree.this, bases[0], *(join.this for join in joins))
    if not 2 <= len(tables) <= 8 or any(type(table) is not exp.Table for table in tables):
        raise ValueError('FROM/USING: одна цель и до 7 прямых источников.')
    names = []
    for table in tables:
        allowed = {'this', 'db', 'catalog', 'alias'} | ({'joins'} if table is bases[0] else set())
        alias = table.args.get('alias')
        if (any(value and key not in allowed for key, value in table.args.items())
                or type(table.this) is not exp.Identifier
                or alias is not None and (type(alias.this) is not exp.Identifier
                    or any(v and k != 'this' for k, v in alias.args.items()))):
            raise ValueError('FROM/USING: параметры таблицы и переименование столбцов не поддерживаются.')
        names.append(_alias(table))
    if len(set(names)) != len(names):
        raise ValueError('FROM/USING: нужны разные имена/псевдонимы цели и источников, в том числе self JOIN.')
    bound = {names[1]}
    for join, name in zip(joins, names[2:]):
        side, kind = join.args.get('side') or '', join.args.get('kind') or ''
        if (any(v and k not in {'this', 'on', 'side', 'kind'} for k, v in join.args.items())
                or (side, kind) not in {('', ''), ('', 'INNER'), ('LEFT', ''), ('LEFT', 'OUTER')}):
            raise ValueError('FROM/USING: разрешены запятая, INNER/LEFT JOIN с ON; остальные виды JOIN исключены.')
        condition = join.args.get('on')
        if condition is None and not side and not kind:
            bound.add(name)
            continue
        terms = tuple(_terms(condition))
        for term in terms:
            columns = (term.this, term.expression) if type(term) is exp.EQ else ()
            if (len(columns) != 2 or any(type(col) is not exp.Column or not col.table or col.db
                    or col.catalog or type(col.this) is not exp.Identifier for col in columns)):
                raise ValueError('FROM/USING ON: нужны равенства alias.column = alias.column, соединённые AND.')
            left, right = (identifier_key(col.args['table']) for col in columns)
            if not (left == name and right in bound or right == name and left in bound):
                raise ValueError('FROM/USING ON: новый источник должен соединяться с предыдущим источником.')
        if not terms:
            raise ValueError('FROM/USING JOIN требует явного ON.')
        bound.add(name)
    return tables


def validate_joined_dml(tree, columns, unique_keys):
    """Bind columns and prove at most one source tuple per target when needed.

    LEFT ON facts may prove only their own nullable source, never the left
    side: a failed LEFT match does not constrain that side. OR facts, computed
    keys, deferred uniqueness and INCLUDE columns are never uniqueness proof.
    """
    tree = tree.copy()
    tree.set('with_', None)
    tables = joined_dml_sources(tree)
    if not tables:
        return
    bindings = {_alias(table): relation_key(table) for table in tables}
    target = _alias(tables[0])

    def resolve(column):
        if type(column) is not exp.Column:
            return None
        if column.catalog:
            raise ValueError('FROM/USING: межбазовые ссылки не поддерживаются.')
        name = identifier_key(column.this) if type(column.this) is exp.Identifier else '*'
        if column.table:
            alias = identifier_key(column.args['table'])
            table = next((t for t in tables if _alias(t) == alias), None)
            if (table is None or column.db and (table.args.get('alias') is not None
                    or identifier_key(column.args['db']) != bindings[alias][0])):
                raise ValueError('FROM/USING: неизвестный или скрытый псевдонимом источник столбца.')
        else:
            matches = [alias for alias, relation in bindings.items() if name in columns[relation]]
            if len(matches) != 1:
                raise ValueError(f'FROM/USING: неизвестный или неоднозначный столбец {name}; укажите псевдоним.')
            alias = matches[0]
        if name != '*' and name not in columns[bindings[alias]]:
            raise ValueError(f'FROM/USING: столбец {name} отсутствует в источнике.')
        return alias, name

    assignments = {id(item.this) for item in tree.expressions} if type(tree) is exp.Update else set()
    for column in tree.find_all(exp.Column):
        if column.find_ancestor(exp.Select) is not None:
            continue  # Inner/correlated columns are bound by the predicate catalog guard.
        if is_default(column):
            continue  # The replay guard separately restricts DEFAULT to SET RHS.
        if id(column) in assignments:
            if (column.table or column.db or column.catalog or type(column.this) is not exp.Identifier
                    or identifier_key(column.this) not in columns[bindings[target]]):
                raise ValueError('UPDATE FROM SET: слева требуется существующий столбец цели без квалификатора.')
        else:
            resolve(column)
    common = list(_terms(tree.args['where'].this))
    private = {}
    for join in tables[1].args.get('joins') or []:
        if join.args.get('side') == 'LEFT':
            private[_alias(join.this)] = list(_terms(join.args.get('on')))
        else:
            common.extend(_terms(join.args.get('on')))

    # Require all sources to be connected, including comma-separated items.
    edges = {alias: set() for alias in bindings}
    for term in (*common, *(t for terms in private.values() for t in terms)):
        if type(term) is exp.EQ and type(term.this) is exp.Column and type(term.expression) is exp.Column:
            a, b = resolve(term.this)[0], resolve(term.expression)[0]
            edges[a].add(b)
            edges[b].add(a)
    connected = {target}
    while True:
        enlarged = connected | set().union(*(edges[a] for a in connected))
        if enlarged == connected:
            break
        connected = enlarged
    if connected != set(bindings):
        raise ValueError('FROM/USING: каждый источник должен быть связан с целью обязательными равенствами; OR не доказывает связь.')

    returning = tree.args.get('returning')
    # PostgreSQL's bare RETURNING * includes FROM/USING columns as well.
    # Only target.* limits the projection to the target row.
    source_output = returning is not None and (
        any(resolve(col)[0] != target for col in returning.find_all(exp.Column))
        or any(type(star.parent) is not exp.Column for star in returning.find_all(exp.Star)))
    if type(tree) is exp.Delete and not source_output:
        return  # PostgreSQL deletes each target once, even with repeated matches.
    bound = {target}
    while True:
        previous = set(bound)
        for alias, relation in bindings.items():
            if alias in bound:
                continue
            keys = set()
            for term in (*common, *private.get(alias, ())):
                if type(term) is not exp.EQ:
                    continue
                for col, other in ((term.this, term.expression), (term.expression, term.this)):
                    if type(col) is not exp.Column or resolve(col)[0] != alias:
                        continue
                    if (type(other) in (exp.Literal, exp.Boolean, exp.Null)
                            or type(other) is exp.Column and resolve(other)[0] in bound):
                        keys.add(resolve(col)[1])
            if any(set(key) <= keys for key in unique_keys.get(relation, ())):
                bound.add(alias)
        if bound == previous:
            break
    if bound != set(bindings):
        names = ', '.join(sorted(set(bindings) - bound))
        raise ValueError(f'FROM/USING: не доказано единственное совпадение для {names}. '
            'Свяжите полный немедленный PRIMARY KEY/UNIQUE обязательными равенствами. '
            'UPDATE и RETURNING столбцов источника при нескольких совпадениях не сравниваются.')


def validate_joined_dml_catalog(connection, program, tables):
    trees = [parse_one(command.sql, read='postgres') for command in program.commands
        if command.kind in {'update', 'delete'}]
    joined = [tree for tree in trees if joined_dml_sources(tree)]
    if not joined:
        return
    columns = {(table.schema, table.name): tuple(name for name, _ in table.columns) for table in tables}
    rows = connection.execute('''SELECT n.nspname,c.relname,ARRAY(
        SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,n)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
        WHERE k.n<=i.indnkeyatts ORDER BY k.n)
        FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=ANY(%s) AND i.indisunique AND i.indimmediate AND i.indisvalid AND i.indisready
        AND i.indexprs IS NULL AND i.indpred IS NULL''',
        (sorted({table.schema for table in tables}),)).fetchall()
    keys = {}
    for schema, table, key in rows:
        if key:
            keys.setdefault((schema, table), []).append(tuple(key))
    for tree in joined:
        catalog = cte_catalog(tree, columns, keys)
        validate_joined_dml(tree, {key: info.columns for key, info in catalog.items()},
            {key: info.keys for key, info in catalog.items()})
