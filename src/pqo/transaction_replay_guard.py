"""Closed SQL/catalog contract for the first disposable-database replay mode."""
from dataclasses import dataclass
from datetime import date, datetime
import re

from sqlglot import exp, parse_one

from .transaction_inspection import inspect_transaction, render_transaction_statement
from .transaction_context import CONTROL_KINDS, TransactionOptions, parse_transaction_control
from .transaction_defaults import ColumnDefault, ReplaySequence, is_default, load_sequences, validate_defaults
from .transaction_upsert import validate_upsert, validate_arbiters, conflict_assignments
from .transaction_joined_dml import joined_dml_sources, validate_joined_dml_catalog
from .transaction_cte import cte_scope, physical_tables, validate_program_ctes


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TYPES = frozenset({'int2', 'int4', 'int8', 'numeric', 'text', 'varchar', 'bool', 'date', 'timestamp', 'timestamptz'})
NODES = frozenset(getattr(exp, name) for name in (
    'Select From Table Identifier TableAlias Column Star Where EQ NEQ LT LTE GT GTE And Or Not Paren Is In Between '
    'Literal Null Boolean Neg Add Sub Mul Div Mod Cast DataType DataTypeParam Order Ordered Limit Alias Count Sum Avg '
    'Min Max Distinct Group Having Join Update Delete Insert Values Tuple Schema Returning With CTE'.split()))
_CATALOG_TYPES = {'int2':exp.DataType.Type.SMALLINT,'int4':exp.DataType.Type.INT,'int8':exp.DataType.Type.BIGINT,
    'numeric':exp.DataType.Type.DECIMAL,'text':exp.DataType.Type.TEXT,'varchar':exp.DataType.Type.VARCHAR,
    'bool':exp.DataType.Type.BOOLEAN,'date':exp.DataType.Type.DATE,'timestamp':exp.DataType.Type.TIMESTAMP,
    'timestamptz':exp.DataType.Type.TIMESTAMPTZ}


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
    options: TransactionOptions = TransactionOptions()
    ending: str = 'commit'

    @property
    def effective_options(self):
        options = self.options
        for command in self.commands:
            if command.kind == 'set_transaction':
                options = options.overlay(parse_transaction_control(command.sql).options)
        return options


@dataclass(frozen=True)
class ReplayTable:
    schema: str
    name: str
    columns: tuple[tuple[str, int], ...]
    primary_key: tuple[str, ...]
    sequences: tuple[ReplaySequence, ...] = ()
    default_signatures: tuple[tuple[str, str, str], ...] = ()


def _identifier_key(identifier):
    """PostgreSQL folds unquoted names; quoted aliases retain their case."""
    return identifier.name if identifier.args.get('quoted') else identifier.name.lower()


def _validate_select_joins(tree):
    """Flat INNER/LEFT equijoins only, each target connected to an earlier source.

    Keep derived tables, lateral sources, implicit cross products and writes
    outside this extension. Composite ON keys are preserved, not truncated.
    """
    joins = tree.args.get('joins') or []
    if not joins:
        return
    source = tree.args.get('from_')
    sources = [source.this if source is not None else None, *(join.this for join in joins)]
    if not 2 <= len(sources) <= 8 or any(type(table) is not exp.Table for table in sources):
        raise ValueError('SELECT с JOIN: нужны 2–8 прямых источников-таблиц, без подзапросов/LATERAL.')
    names = []
    for table in sources:
        if any(value and key not in {'this', 'db', 'catalog', 'alias'} for key, value in table.args.items()):
            raise ValueError('Дополнительные параметры таблицы JOIN не поддерживаются.')
        alias = table.args.get('alias')
        if alias is not None and (alias.args.get('columns') or type(alias.this) is not exp.Identifier):
            raise ValueError('JOIN: переименование столбцов источника не поддерживается.')
        identifier = alias.this if alias is not None else table.this
        if type(identifier) is not exp.Identifier:
            raise ValueError('JOIN требует простых имён таблиц или псевдонимов.')
        names.append(_identifier_key(identifier))
    if len(set(names)) != len(names):
        raise ValueError('Источники JOIN должны иметь разные имена/псевдонимы; для self JOIN задайте разные псевдонимы.')
    bound = {names[0]}
    for join, target in zip(joins, names[1:]):
        side, kind = join.args.get('side') or '', join.args.get('kind') or ''
        if (any(value and key not in {'this', 'on', 'side', 'kind'} for key, value in join.args.items())
                or (side, kind) not in {('', ''), ('', 'INNER'), ('LEFT', ''), ('LEFT', 'OUTER')}):
            raise ValueError('Разрешены только INNER/LEFT JOIN с явным ON; CROSS/RIGHT/FULL/NATURAL/USING исключены.')
        def connected(condition):
            if type(condition) is exp.Paren:
                return connected(condition.this)
            if type(condition) is exp.And:
                return connected(condition.this) and connected(condition.expression)
            if type(condition) is not exp.EQ:
                return False
            left, right = condition.this, condition.expression
            if any(type(column) is not exp.Column or not column.table or column.db or column.catalog
                    or type(column.this) is not exp.Identifier for column in (left, right)):
                return False
            a, b = (_identifier_key(column.args['table']) for column in (left, right))
            return a == target and b in bound or b == target and a in bound
        if not connected(join.args.get('on')):
            raise ValueError('ON должен соединять новую таблицу с предыдущими через alias.column = alias.column; составные ключи — через AND.')
        bound.add(target)


def prepare_replay(text, settings, *, allow_select_wrapper=False):
    """Never authorize arbitrary SQL by its leading keyword; exact AST allowlist."""
    if len(text) > 50_000:
        raise ValueError('Первый режим: не более 50000 символов SQL.')
    inspection = inspect_transaction(text)
    if inspection.unsupported_commands or len(inspection.statements) > 20:
        raise ValueError('Поддерживается BEGIN … COMMIT/ROLLBACK и до 20 разрешённых команд.')
    kinds = {item.kind for item in inspection.statements}
    writes = bool(kinds.intersection({'insert','update','delete'}))
    if inspection.effective_options.read_only and writes:
        raise ValueError('READ ONLY запрещает INSERT/UPDATE/DELETE; режим не будет заменён на READ WRITE.')
    if 'select' not in kinds or not writes and inspection.effective_options.read_only is not True:
        raise ValueError('Нужны SELECT и команда записи либо явно заданная транзакция READ ONLY.')
    commands, tables, savepoints = [], set(), []
    for item in inspection.statements:
        if item.kind in CONTROL_KINDS:
            if item.kind == 'savepoint':
                savepoints.append(item.savepoint)
            elif item.kind in {'rollback_to','release'}:
                if item.savepoint not in savepoints:
                    raise ValueError(f'Команда {item.number}: нет активной точки сохранения с таким именем.')
                index = len(savepoints)-1-savepoints[::-1].index(item.savepoint)
                savepoints = savepoints[:index+(1 if item.kind=='rollback_to' else 0)]
            commands.append(ReplayCommand(item.number,item.kind,item.sql,False))
            continue
        tree = parse_one(item.sql, read='postgres')
        # Parameter binding emits qualified built-in types. SQLGlot represents
        # them as USERDEFINED/Dot; normalize only this closed pg_catalog map.
        for datatype in tree.find_all(exp.DataType):
            kind = datatype.args.get('kind')
            if (datatype.this == exp.DataType.Type.USERDEFINED and type(kind) is exp.Dot
                    and type(kind.this) is exp.Identifier and type(kind.expression) is exp.Identifier
                    and _identifier_key(kind.this)=='pg_catalog' and _identifier_key(kind.expression) in _CATALOG_TYPES):
                datatype.set('this',_CATALOG_TYPES[_identifier_key(kind.expression)])
                datatype.set('kind',None)
        if type(tree) not in (exp.Select, exp.Insert, exp.Update, exp.Delete):
            raise ValueError(f'Команда {item.number}: неподдерживаемый корень SQL.')
        scope = cte_scope(tree)
        cte_selects = tuple(definition.this for definition in scope.definitions)
        if scope.definitions:
            for select in (node for node in tree.walk() if type(node) is exp.Select):
                source = select.args.get('from_')
                if (source is not None and type(source.this) is not exp.Table
                        or len([t for t in select.find_all(exp.Table) if t.find_ancestor(exp.Select) is select]) > 8):
                    raise ValueError('CTE: каждый SELECT допускает до 8 прямых источников, без FROM-подвыборок.')
        conflict_action=validate_upsert(tree)
        joined_write = joined_dml_sources(tree)
        insert_select=tree.expression if isinstance(tree,exp.Insert) and type(tree.expression) is exp.Select else None
        if insert_select is not None:
            source=insert_select.args.get('from_')
            if source is not None and type(source.this) is not exp.Table:
                raise ValueError('INSERT SELECT: источник — прямая таблица или INNER/LEFT JOIN, без подвыборок.')
            if len(list(insert_select.find_all(exp.Table)))>8:
                raise ValueError('INSERT SELECT: не более 8 прямых источников.')
            for table in insert_select.find_all(exp.Table):
                alias=table.args.get('alias')
                if (any(value and key not in {'this','db','catalog','alias'} for key,value in table.args.items())
                        or alias is not None and (type(alias.this) is not exp.Identifier or alias.args.get('columns'))):
                    raise ValueError('INSERT SELECT: дополнительные параметры источников/переименование столбцов не поддерживаются.')
        # Rewrite-only extension: exactly one derived SELECT in FROM, under an
        # outer aggregate. It cannot be combined with joins or other subqueries.
        wrapper = tree.args.get('from_').this if isinstance(tree,exp.Select) and tree.args.get('from_') else None
        inner = wrapper.this if allow_select_wrapper and isinstance(wrapper,exp.Subquery) else None
        wrapper_ok = (isinstance(inner,exp.Select) and bool(wrapper.alias)
            and not wrapper.args.get('alias').args.get('columns')
            and not any(value for key,value in wrapper.args.items() if key not in {'this','alias'})
            and len(list(tree.find_all(exp.Select))) == 2
            and any(isinstance(node,exp.AggFunc) for expression in tree.expressions for node in expression.walk()))
        if wrapper_ok and tree.find(exp.Join) is not None:
            raise ValueError('FROM-подвыборка под агрегатом пока поддерживается только без JOIN.')
        selects = {'expressions', 'from_', 'joins', 'where', 'group', 'having', 'order', 'limit', 'distinct'}
        for node in tree.walk():
            default_value = is_default(node)
            if default_value:
                valid = (isinstance(tree,exp.Insert) and type(node.parent) is exp.Tuple
                    and type(node.parent.parent) is exp.Values and node.parent.parent is tree.expression
                    or isinstance(tree,exp.Update) and type(node.parent) is exp.EQ
                    and node.parent in tree.expressions and node.parent.expression is node
                    or isinstance(tree,exp.Insert) and type(node.parent) is exp.EQ
                    and node.parent in conflict_assignments(tree) and node.parent.expression is node)
                if not valid:
                    raise ValueError('DEFAULT допускается только отдельным значением INSERT/UPDATE.')
            conflict_node = isinstance(tree,exp.Insert) and node is tree.args.get('conflict')
            if type(node) not in NODES and not (wrapper_ok and node is wrapper) and not default_value and not conflict_node and node is not conflict_action:
                raise ValueError(f'Команда {item.number}: конструкция {type(node).__name__} не разрешена для выполнения.')
            if isinstance(node, exp.Identifier) and not NAME.fullmatch(node.name):
                raise ValueError('Первый режим требует простых идентификаторов до 63 ASCII-символов.')
            if isinstance(node, exp.Literal) and node.is_string and node.this.casefold() in {
                'now', 'today', 'tomorrow', 'yesterday', 'infinity', '-infinity', 'current_date', 'current_timestamp',
            }:
                raise ValueError('Специальные временные литералы исключены, в том числе при неявном приведении.')
            if isinstance(node, exp.Column) and node.name.casefold() in {'ctid','xmin','xmax','cmin','cmax','tableoid'}:
                raise ValueError('Системные физические столбцы не являются переносимым результатом между копиями.')
            if isinstance(node, exp.Select) and node is not tree and node is not insert_select and all(node is not allowed for allowed in cte_selects) and not (wrapper_ok and node is inner):
                raise ValueError('Подзапросы вне определений SELECT CTE пока не выполняются.')
            if isinstance(node,exp.Select):
                if any(value and key not in selects and not (node is tree and key == 'with_') for key,value in node.args.items()):
                    raise ValueError('Дополнительные параметры SELECT не поддерживаются.')
                if node.args.get('limit') and not node.args.get('order'):
                    raise ValueError('SELECT с LIMIT требует явного ORDER BY.')
                _validate_select_joins(node)
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
                if isinstance(value, exp.Null):
                    continue
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
        maximum=9 if insert_select is not None else 8 if isinstance(tree,exp.Select) or joined_write else 1
        main_tables = [t for t in found if t.find_ancestor(exp.CTE) is None]
        if (not scope.definitions and not 1 <= len(found) <= maximum
                or scope.definitions and not 1 <= len(main_tables) <= maximum):
            raise ValueError('SELECT: до 8 источников; INSERT SELECT: цель и до 8 источников; UPDATE FROM/DELETE USING: цель и до 7 источников.')
        for table in physical_tables(tree):
            if table.catalog or table.db not in settings.allowed_schemas or not isinstance(table.this, exp.Identifier):
                raise ValueError('Каждая таблица должна иметь явную разрешённую схему.')
            tables.add((table.db, table.name))
        if isinstance(tree, (exp.Update, exp.Delete)) and tree.args.get('where') is None:
            raise ValueError('UPDATE/DELETE без WHERE пока не выполняются.')
        if isinstance(tree, exp.Insert):
            if (not isinstance(tree.this,(exp.Table,exp.Schema))
                    or not (tree.args.get('default') and tree.expression is None or isinstance(tree.expression,exp.Values) or insert_select is not None)):
                raise ValueError('INSERT: поддерживаются VALUES, DEFAULT VALUES и прямой SELECT, без OVERRIDING/CTE/UNION.')
            if isinstance(tree.expression,exp.Values) and (not tree.expression.expressions or len(tree.expression.expressions) > 100):
                raise ValueError('INSERT: не более 100 строк VALUES.')
        if isinstance(tree,exp.Update) and any(type(item) is not exp.EQ or type(item.this) is not exp.Column
                for item in tree.expressions):
            raise ValueError('UPDATE: поддерживаются только отдельные присваивания столбцам, без кортежей.')
        if isinstance(tree, exp.Select) and tree.args.get('limit') and not tree.args.get('order'):
            raise ValueError('SELECT с LIMIT требует явного ORDER BY; несовпадения при равных ключах тоже отклоняются.')
        # Unknown truthy root options must never slip through the node allowlist.
        allowed = (selects if isinstance(tree, exp.Select)
            else {'this', 'expressions', 'where', 'returning', 'from_'} if isinstance(tree, exp.Update)
            else {'this', 'where', 'returning', 'using'} if isinstance(tree, exp.Delete)
            else {'this', 'expression', 'returning', 'default', 'conflict'})
        if any(value and key not in allowed and key != 'with_' for key, value in tree.args.items()):
            raise ValueError(f'Команда {item.number}: дополнительные параметры пока не поддерживаются.')
        commands.append(ReplayCommand(item.number,item.kind,render_transaction_statement(tree),bool(tree.args.get('order'))))
    return ReplayProgram(tuple(commands), tuple(sorted(tables)),inspection.options,inspection.ending)


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
    sequence_rows = [row for row in relations if row[3]=='S']
    relations = [row for row in relations if row[3]!='S']
    if not 1 <= len(relations) <= 8:
        raise ValueError('Первый режим: 1–8 таблиц в разрешённых схемах.')
    sequences = load_sequences(connection, relations, sequence_rows)
    oids = {row[0] for row in relations}
    if not set(program.tables) <= {(row[1], row[2]) for row in relations}:
        raise ValueError('Не все таблицы SQL найдены в лабораторной копии.')
    # Resolve SELECT */alias.* only from catalogs. No source rows/defaults are
    # evaluated to determine INSERT's projection width and omitted columns.
    column_rows=connection.execute('''SELECT attrelid,attname FROM pg_catalog.pg_attribute
        WHERE attrelid=ANY(%s) AND attnum>0 AND NOT attisdropped ORDER BY attrelid,attnum''',
        (sorted(oids),)).fetchall()
    source_columns={(row[1],row[2]):tuple(name for oid,name in column_rows if oid==row[0]) for row in relations}
    validate_program_ctes(program, source_columns)
    tables = []
    for oid, schema, name, kind, persistence, rls, force_rls, partition, descendants, rules, method in relations:
        if (kind != 'r' or persistence != 'p' or method != 'heap' or rls or force_rls or partition or descendants or rules):
            raise ValueError(f'{schema}.{name}: разрешены только обычные постоянные heap-таблицы без RLS/наследования/правил.')
        columns = connection.execute("""SELECT a.attname,a.atttypid,t.typname,tn.nspname,a.atthasdef,a.attidentity,a.attgenerated,cn.nspname,
            a.attnum,ad.oid,ad.adbin::text
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
            LEFT JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation
            LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=co.collnamespace
            LEFT JOIN pg_catalog.pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall()
        if not columns or any(col[2] not in TYPES or col[3] != 'pg_catalog' or col[6]
                or col[7] not in (None,'pg_catalog') or bool(col[4]) != bool(col[9]) for col in columns):
            raise ValueError(f'{schema}.{name}: типы, генерация, DEFAULT-каталог либо collation вне поддерживаемого контракта.')
        defaults = validate_defaults(connection,oid,schema,name,
            tuple(ColumnDefault(col[0],col[8],col[5],col[9],col[10]) for col in columns),program,sequences[oid],source_columns=source_columns)
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
        validate_arbiters(connection,oid,schema,name,tuple(col[0] for col in columns),program)
        tables.append(ReplayTable(schema,name,tuple((col[0],col[1]) for col in columns),tuple(row[0] for row in key),sequences[oid],defaults))
    validate_joined_dml_catalog(connection, program, tables)
    return tuple(tables)
