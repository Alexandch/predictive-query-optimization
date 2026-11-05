"""Closed SQL/catalog contract for the first disposable-database replay mode."""
from dataclasses import dataclass
from datetime import date, datetime
import re

from sqlglot import exp, parse_one

from .transaction_inspection import inspect_transaction


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TYPES = frozenset({'int2', 'int4', 'int8', 'numeric', 'text', 'varchar', 'bool', 'date', 'timestamp', 'timestamptz'})
NODES = frozenset(getattr(exp, name) for name in (
    'Select From Table Identifier TableAlias Column Star Where EQ NEQ LT LTE GT GTE And Or Not Paren Is In Between '
    'Literal Null Boolean Neg Add Sub Mul Div Mod Cast DataType DataTypeParam Order Ordered Limit Alias Count Sum Avg '
    'Min Max Distinct Group Having Update Delete Insert Values Tuple Schema Returning'.split()))


@dataclass(frozen=True)
class ReplayCommand:
    number: int
    kind: str
    sql: str
    ordered: bool


@dataclass(frozen=True)
class ReplayProgram:
    commands: tuple[ReplayCommand, ...]
    tables: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReplayTable:
    schema: str
    name: str
    columns: tuple[tuple[str, int], ...]
    primary_key: tuple[str, ...]


def prepare_replay(text, settings, *, allow_select_wrapper=False):
    """Never authorize arbitrary SQL by its leading keyword; exact AST allowlist."""
    if len(text) > 50_000:
        raise ValueError('Первый режим: не более 50000 символов SQL.')
    inspection = inspect_transaction(text)
    if inspection.ending != 'commit' or inspection.unsupported_commands or len(inspection.statements) > 20:
        raise ValueError('Первый режим поддерживает только BEGIN … COMMIT и 2–20 простых команд; ROLLBACK/контекст пока исключены.')
    kinds = {item.kind for item in inspection.statements}
    if 'select' not in kinds or not kinds.intersection({'insert', 'update', 'delete'}):
        raise ValueError('Нужна смешанная транзакция: хотя бы один SELECT и одна простая команда записи.')
    commands, tables = [], set()
    for item in inspection.statements:
        tree = parse_one(item.sql, read='postgres')
        if type(tree) not in (exp.Select, exp.Insert, exp.Update, exp.Delete):
            raise ValueError(f'Команда {item.number}: неподдерживаемый корень SQL.')
        # Rewrite-only extension: exactly one derived SELECT in FROM, under an
        # outer aggregate. No scalar/correlated subqueries, joins or CTEs.
        wrapper = tree.args.get('from_').this if isinstance(tree,exp.Select) and tree.args.get('from_') else None
        inner = wrapper.this if allow_select_wrapper and isinstance(wrapper,exp.Subquery) else None
        wrapper_ok = (isinstance(inner,exp.Select) and bool(wrapper.alias)
            and not wrapper.args.get('alias').args.get('columns')
            and not any(value for key,value in wrapper.args.items() if key not in {'this','alias'})
            and len(list(tree.find_all(exp.Select))) == 2
            and any(isinstance(node,exp.AggFunc) for expression in tree.expressions for node in expression.walk()))
        selects = {'expressions', 'from_', 'where', 'group', 'having', 'order', 'limit', 'distinct'}
        for node in tree.walk():
            if type(node) not in NODES and not (wrapper_ok and node is wrapper):
                raise ValueError(f'Команда {item.number}: конструкция {type(node).__name__} не разрешена для выполнения.')
            if isinstance(node, exp.Identifier) and not NAME.fullmatch(node.name):
                raise ValueError('Первый режим требует простых идентификаторов до 63 ASCII-символов.')
            if isinstance(node, exp.Literal) and node.is_string and node.this.casefold() in {
                'now', 'today', 'tomorrow', 'yesterday', 'infinity', '-infinity', 'current_date', 'current_timestamp',
            }:
                raise ValueError('Специальные временные литералы исключены, в том числе при неявном приведении.')
            if isinstance(node, exp.Column) and node.name.casefold() in {'ctid','xmin','xmax','cmin','cmax','tableoid'}:
                raise ValueError('Системные физические столбцы не являются переносимым результатом между копиями.')
            if isinstance(node, exp.Select) and node is not tree and not (wrapper_ok and node is inner):
                raise ValueError('Вложенные SELECT/CTE пока не выполняются.')
            if isinstance(node,exp.Select):
                if any(value and key not in selects for key,value in node.args.items()):
                    raise ValueError('Дополнительные параметры SELECT не поддерживаются.')
                if node.args.get('limit') and not node.args.get('order'):
                    raise ValueError('SELECT с LIMIT требует явного ORDER BY.')
            if isinstance(node, exp.Distinct) and node.args.get('on'):
                raise ValueError('DISTINCT ON пока не выполняется.')
            if isinstance(node, exp.Star) and any(node.args.values()):
                raise ValueError('Модификаторы звёздочки пока не поддерживаются.')
            if isinstance(node, exp.DataType) and node.this not in {
                exp.DataType.Type.SMALLINT, exp.DataType.Type.INT, exp.DataType.Type.BIGINT,
                exp.DataType.Type.DECIMAL, exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR,
                exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP,
                exp.DataType.Type.TIMESTAMPTZ,
            }:
                raise ValueError('Неподдерживаемый тип приведения.')
            if isinstance(node, exp.Cast) and node.args['to'].this in {
                exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ,
            }:
                value = node.this
                if not isinstance(value, exp.Literal) or not value.is_string:
                    raise ValueError('Дата/время должны быть явным ISO-литералом, без now/today и вычислений.')
                try:
                    if node.args['to'].this == exp.DataType.Type.DATE:
                        date.fromisoformat(value.this)
                    else:
                        datetime.fromisoformat(value.this)
                except ValueError as exc:
                    raise ValueError('Дата/время должны быть явным ISO-литералом.') from exc
        found = list(tree.find_all(exp.Table))
        if len(found) != 1:
            raise ValueError('Первый режим: ровно одна физическая таблица в каждой команде, без JOIN/подзапросов.')
        table = found[0]
        if table.catalog or table.db not in settings.allowed_schemas or not isinstance(table.this, exp.Identifier):
            raise ValueError('Каждая таблица должна иметь явную разрешённую схему.')
        tables.add((table.db, table.name))
        if isinstance(tree, (exp.Update, exp.Delete)) and tree.args.get('where') is None:
            raise ValueError('UPDATE/DELETE без WHERE пока не выполняются.')
        if isinstance(tree, exp.Insert):
            if not isinstance(tree.this, exp.Schema) or not tree.this.expressions or not isinstance(tree.expression, exp.Values):
                raise ValueError('INSERT: нужен явный список столбцов и VALUES, без SELECT/DEFAULT/ON CONFLICT.')
            if len(tree.expression.expressions) > 100:
                raise ValueError('INSERT: не более 100 строк VALUES.')
        if isinstance(tree, exp.Select) and tree.args.get('limit') and not tree.args.get('order'):
            raise ValueError('SELECT с LIMIT требует явного ORDER BY; несовпадения при равных ключах тоже отклоняются.')
        # Unknown truthy root options must never slip through the node allowlist.
        allowed = (selects if isinstance(tree, exp.Select)
            else {'this', 'expressions', 'where', 'returning'} if isinstance(tree, exp.Update)
            else {'this', 'where', 'returning'} if isinstance(tree, exp.Delete)
            else {'this', 'expression', 'returning'})
        if any(value and key not in allowed for key, value in tree.args.items()):
            raise ValueError(f'Команда {item.number}: дополнительные параметры пока не поддерживаются.')
        commands.append(ReplayCommand(item.number, item.kind, tree.sql(dialect='postgres'), bool(tree.args.get('order'))))
    return ReplayProgram(tuple(commands), tuple(sorted(tables)))


def replay_catalog(connection, settings, program):
    """Read only catalogs before any user expression/index/state scan is run.

    This is a conservative contract for a TRUSTED laboratory server, not a
    security sandbox for hostile PostgreSQL/server configuration or superusers.
    Validate every table in allowed schemas, not only explicit write targets.
    """
    if connection.execute("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger WHERE evtenabled <> 'D') OR EXISTS (SELECT 1 FROM pg_catalog.pg_subscription WHERE subdbid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())) OR EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname<>'plpgsql')").fetchone()[0]:
        raise ValueError('Расширения, подписки и активные event triggers исключены из первого режима.')
    relations = connection.execute("""SELECT c.oid,n.nspname,c.relname,c.relkind,c.relpersistence,
        c.relrowsecurity,c.relforcerowsecurity,c.relispartition,c.relhassubclass,c.relhasrules,a.amname
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_am a ON a.oid=c.relam
        WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','f','S') ORDER BY n.nspname,c.relname""",
        (sorted(settings.allowed_schemas),)).fetchall()
    if not 1 <= len(relations) <= 8:
        raise ValueError('Первый режим: 1–8 таблиц в разрешённых схемах.')
    oids = {row[0] for row in relations}
    if not set(program.tables) <= {(row[1], row[2]) for row in relations}:
        raise ValueError('Не все таблицы SQL найдены в лабораторной копии.')
    tables = []
    for oid, schema, name, kind, persistence, rls, force_rls, partition, descendants, rules, method in relations:
        if (kind != 'r' or persistence != 'p' or method != 'heap' or rls or force_rls or partition or descendants or rules):
            raise ValueError(f'{schema}.{name}: разрешены только обычные постоянные heap-таблицы без RLS/наследования/правил.')
        columns = connection.execute("""SELECT a.attname,a.atttypid,t.typname,tn.nspname,a.atthasdef,a.attidentity,a.attgenerated,cn.nspname
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
            LEFT JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation
            LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=co.collnamespace
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall()
        if not columns or any(type_name not in TYPES or ns != 'pg_catalog' or default or identity or generated
                or collation_ns not in (None, 'pg_catalog') for _, _, type_name, ns, default, identity, generated, collation_ns in columns):
            raise ValueError(f'{schema}.{name}: типы, DEFAULT, identity/генерация либо collation вне поддерживаемого контракта.')
        constraints = connection.execute("SELECT contype,confrelid,confupdtype,confdeltype FROM pg_catalog.pg_constraint WHERE conrelid=%s", (oid,)).fetchall()
        if any(kind not in ('p', 'u', 'f') or kind == 'f' and (target not in oids or update not in ('a','r') or delete not in ('a','r'))
                for kind, target, update, delete in constraints):
            raise ValueError(f'{schema}.{name}: CHECK/исключения, внешние FK или каскады пока не поддерживаются.')
        # Also catch inbound FKs from schemas outside the fingerprinted state.
        if connection.execute("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint WHERE contype='f' AND confrelid=%s AND NOT (conrelid=ANY(%s)))", (oid, sorted(oids))).fetchone()[0]:
            raise ValueError('Входящий FK вне проверяемых таблиц.')
        if connection.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t
            LEFT JOIN pg_catalog.pg_constraint c ON c.oid=t.tgconstraint
            JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE t.tgrelid=%s AND t.tgenabled<>'D' AND
            (NOT t.tgisinternal OR c.contype IS DISTINCT FROM 'f' OR n.nspname<>'pg_catalog' OR
             p.proname NOT IN ('RI_FKey_check_ins','RI_FKey_check_upd','RI_FKey_noaction_del','RI_FKey_noaction_upd','RI_FKey_restrict_del','RI_FKey_restrict_upd')))
            """, (oid,)).fetchone()[0]:
            raise ValueError(f'{schema}.{name}: пользовательские/неизвестные триггеры исключены.')
        if connection.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid JOIN pg_catalog.pg_am a ON a.oid=c.relam
            WHERE i.indrelid=%s AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL OR NOT i.indisvalid OR NOT i.indisready
             OR a.amname<>'btree' OR EXISTS(SELECT 1 FROM unnest(i.indclass) op(oid)
                JOIN pg_catalog.pg_opclass cl ON cl.oid=op.oid JOIN pg_catalog.pg_namespace n ON n.oid=cl.opcnamespace
                WHERE n.nspname<>'pg_catalog' OR NOT cl.opcdefault)
             OR EXISTS(SELECT 1 FROM unnest(i.indcollation) co(oid) JOIN pg_catalog.pg_collation c ON c.oid=co.oid
                JOIN pg_catalog.pg_namespace n ON n.oid=c.collnamespace WHERE n.nspname<>'pg_catalog')))
            """, (oid,)).fetchone()[0]:
            raise ValueError(f'{schema}.{name}: поддерживаются только обычные полные B-tree с default opclass.')
        key = connection.execute("""SELECT a.attname FROM pg_catalog.pg_index i
            CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,n)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
            WHERE i.indrelid=%s AND i.indisprimary AND k.n<=i.indnkeyatts ORDER BY k.n""", (oid,)).fetchall()
        if not key:
            raise ValueError(f'{schema}.{name}: нужен PRIMARY KEY для однозначного снимка состояния.')
        tables.append(ReplayTable(schema, name, tuple((col[0], col[1]) for col in columns), tuple(row[0] for row in key)))
    return tuple(tables)
