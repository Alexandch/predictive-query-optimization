"""Predicate SELECT scopes for guarded replay; no scalar subqueries or probes."""
from sqlglot import exp

from .transaction_cte import cte_catalog, relation_key
from .transaction_defaults import identifier_key


def direct_sources(context):
    if type(context) is exp.Select:
        source = context.args.get('from_')
        return ([source.this] if source else []) + [j.this for j in context.args.get('joins') or []]
    if type(context) in (exp.Update, exp.Delete):
        from .transaction_joined_dml import joined_dml_sources
        return list(joined_dml_sources(context) or (context.this,))
    return []


def predicate_selects(tree):
    """Accept EXISTS/one-column IN only in WHERE/HAVING boolean expressions.

    NULL-sensitive NOT IN is kept verbatim. Boolean projections, assignments,
    RETURNING, scalar/row subqueries and conflict clauses remain outside this
    stage. The general replay AST allowlist still validates every inner node.
    """
    queries = []
    for node in tree.walk():
        if type(node) is exp.Exists:
            query = node.this
        elif type(node) is exp.In and node.args.get('query') is not None:
            wrapper = node.args['query']
            if (type(wrapper) is not exp.Subquery
                    or any(v and k != 'this' for k, v in wrapper.args.items())):
                raise ValueError('IN: нужен один SELECT без дополнительных параметров подзапроса.')
            query = wrapper.this
            if (type(query) is not exp.Select or len(query.expressions) != 1
                    or type(node.this) is exp.Tuple
                    or type(query.expressions[0]) is exp.Star
                    or type(query.expressions[0]) is exp.Column and query.expressions[0].is_star):
                raise ValueError('IN: поддерживается одно значение и один выходной столбец, без кортежей и звёздочки.')
        else:
            continue
        if type(query) is not exp.Select:
            raise ValueError('EXISTS/IN: поддерживаются только SELECT, без UNION и команд записи.')
        parent = node.parent
        while parent is not None and type(parent) not in (exp.Where, exp.Having, exp.Select, exp.Returning, exp.Update, exp.Delete, exp.Insert):
            parent = parent.parent
        if type(parent) not in (exp.Where, exp.Having) or node.find_ancestor(exp.OnConflict) is not None:
            raise ValueError('Подзапросы EXISTS/IN допускаются только в WHERE/HAVING, без SET/RETURNING/ON CONFLICT.')
        sources = direct_sources(query)
        if len(sources) > 8 or any(type(source) is not exp.Table for source in sources):
            raise ValueError('Подзапрос: до 8 прямых источников, без FROM-подвыборок и LATERAL.')
        for table in sources:
            alias = table.args.get('alias')
            if (any(v and k not in {'this', 'db', 'catalog', 'alias'} for k, v in table.args.items())
                    or alias is not None and (type(alias.this) is not exp.Identifier
                        or any(v and k != 'this' for k, v in alias.args.items()))):
                raise ValueError('Подзапрос: параметры источников и переименование столбцов не поддерживаются.')
        queries.append(query)
    if len(queries) > 16 or queries and len(list(tree.find_all(exp.Table))) > 64:
        raise ValueError('Не более 16 предикатных подзапросов и 64 ссылок на источники в команде.')
    identities = {id(query) for query in queries}
    for query in queries:
        depth, parent = 0, query
        while parent is not None:
            depth += id(parent) in identities
            parent = parent.parent
        if depth > 3:
            raise ValueError('Предикатные подзапросы: не более трёх уровней вложенности.')
    return tuple(queries)


def validate_predicate_columns(tree, physical_columns):
    """Bind inner columns lexically, respecting shadowing and CTE boundaries.

    Catalogs, never sample rows, decide which query owns a column. A local alias
    hides an outer alias even when the requested local column does not exist.
    """
    queries = predicate_selects(tree)
    if not queries:
        return
    catalog = cte_catalog(tree, physical_columns)
    cache = {}

    def bindings(context):
        if id(context) not in cache:
            items = {}
            for table in direct_sources(context):
                alias = table.args.get('alias')
                name = identifier_key(alias.this if alias else table.this)
                relation = relation_key(table)
                if name in items or relation not in catalog:
                    raise ValueError('Подзапрос: неизвестный источник или повторный псевдоним.')
                items[name] = (table, catalog[relation].columns)
            cache[id(context)] = items
        return cache[id(context)]

    def outer(context):
        parent = context.parent
        while parent is not None:
            if type(parent) in (exp.CTE, exp.Insert):
                return None
            if type(parent) in (exp.Select, exp.Update, exp.Delete):
                return parent
            parent = parent.parent
        return None

    for query in queries:
        local = bindings(query)
        for column in query.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not query:
                continue
            if column.catalog:
                raise ValueError('Подзапрос: межбазовые ссылки не поддерживаются.')
            name = identifier_key(column.this) if type(column.this) is exp.Identifier else '*'
            # PostgreSQL allows SELECT output aliases in ORDER BY. Other
            # clauses resolve source columns, with normal local precedence.
            order = query.args.get('order')
            if not column.table and order is not None and column.find_ancestor(exp.Order) is order:
                aliases = [identifier_key(p.args['alias']) for p in query.expressions if type(p) is exp.Alias]
                if aliases.count(name) == 1:
                    continue
            context = query
            while context is not None:
                sources = local if context is query else bindings(context)
                if column.table:
                    item = sources.get(identifier_key(column.args['table']))
                    if item is not None:
                        table, names = item
                        if (column.db and (table.args.get('alias') is not None
                                or identifier_key(column.args['db']) != table.db)
                                or name != '*' and name not in names):
                            raise ValueError('Подзапрос: столбец отсутствует в локальном источнике или скрыт псевдонимом.')
                        break
                else:
                    matches = [item for item in sources.values() if name in item[1]]
                    if len(matches) > 1:
                        raise ValueError('Подзапрос: неоднозначный столбец; укажите псевдоним.')
                    if matches:
                        break
                context = outer(context)
            else:
                raise ValueError('Подзапрос: неизвестный столбец или недоступная внешняя область видимости.')
