"""Reachable DEFAULT audit and owned sequence metadata; never evaluates a default."""
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256

from sqlglot import exp, parse_one
from .transaction_upsert import conflict_assignments


@dataclass(frozen=True)
class ReplaySequence:
    schema: str
    name: str
    column: str
    oid: int
    owner_oid: int
    position: int
    dependency: str
    configuration: tuple[int | bool, ...]


@dataclass(frozen=True)
class ColumnDefault:
    name: str
    position: int
    identity: str
    definition_oid: int | None
    definition_tree: str | None


def identifier_key(node):
    return node.name if node.args.get('quoted') else node.name.lower()


def is_default(node):
    return (type(node) is exp.Var and node.this.upper()=='DEFAULT'
        or type(node) is exp.Column and not any(node.args.get(k) for k in ('table','db','catalog'))
        and type(node.this) is exp.Identifier and not node.this.args.get('quoted') and node.name.upper()=='DEFAULT')


def select_projection_width(source,source_columns):
    """Expand only direct * and alias.* using already guarded flat sources."""
    from .transaction_predicates import direct_sources
    sources=direct_sources(source)
    bindings={identifier_key(table.args['alias'].this if table.args.get('alias') else table.this):table for table in sources}
    count=0
    for expression in source.expressions:
        if type(expression) is exp.Star:
            tables=sources
        elif type(expression) is exp.Column and type(expression.this) is exp.Star:
            table=bindings.get(identifier_key(expression.args['table'])) if expression.table else None
            if table is None or expression.catalog or expression.db and expression.db!=table.db:
                raise ValueError('INSERT SELECT: звёздочка не разрешена по источникам.')
            tables=[table]
        else:
            count+=1
            continue
        from .transaction_cte import relation_key
        if not tables or source_columns is None or any(relation_key(t) not in source_columns for t in tables):
            raise ValueError('INSERT SELECT: для звёздочки нужны известные столбцы прямых источников.')
        count+=sum(len(source_columns[relation_key(t)]) for t in tables)
    if count==0:raise ValueError('INSERT SELECT: список выходных столбцов пуст.')
    return count


def used_defaults(program, schema, table, columns, *, source_columns=None):
    """Missing INSERT values and exact DEFAULT assignments are the only callers.

    Tuple assignments and overriding are rejected by the guard. INSERT SELECT
    supplies a known projection width; only omitted target columns use defaults.
    Defaults in the conditional DO UPDATE branch are audited even when its
    condition currently looks false; the branch is not evaluated during audit.
    """
    active=set()
    for command in program.commands:
        if command.kind not in {'insert','update'}:continue
        tree=parse_one(command.sql,read='postgres')
        target=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
        if (target.db,target.name)!=(schema,table):continue
        if isinstance(tree,exp.Update):
            active.update(identifier_key(item.this.this) for item in tree.expressions if is_default(item.expression))
            continue
        active.update(identifier_key(item.this.this) for item in conflict_assignments(tree) if is_default(item.expression))
        if tree.args.get('default'):
            active.update(columns)
            continue
        if type(tree.expression) is exp.Select:
            if tree.args.get('with_'):
                from .transaction_cte import cte_catalog
                available = {key: info.columns for key, info in cte_catalog(tree, source_columns).items()}
            else:
                available = source_columns
            count=select_projection_width(tree.expression,available)
            rows=()
        else:
            rows=tree.expression.expressions
            lengths={len(row.expressions) for row in rows}
            if len(lengths)!=1:
                raise ValueError('INSERT: строки VALUES должны иметь одинаковую длину.')
            count=next(iter(lengths))
        explicit=(tree.this.expressions if isinstance(tree.this,exp.Schema)
            else target.args.get('alias').args.get('columns') if target.args.get('alias') else None)
        names=[identifier_key(node) for node in explicit] if explicit else list(columns[:count])
        if len(names)!=count or len(set(names))!=len(names) or not set(names)<=set(columns):
            raise ValueError('INSERT: проверьте число значений и имена столбцов.')
        active.update(set(columns)-set(names))
        for row in rows:
            active.update(name for name,node in zip(names,row.expressions) if is_default(node))
    if not active<=set(columns):raise ValueError('UPDATE DEFAULT: неизвестный столбец.')
    return active


def load_sequences(connection, tables, sequences):
    owners={row[0]:(row[1],row[2]) for row in tables}
    result={oid:[] for oid in owners}
    if len(sequences)>32:raise ValueError('Для проверки допускается до 32 собственных последовательностей.')
    for oid,schema,name,kind,persistence,*_ in sequences:
        dependencies=connection.execute("""SELECT refobjid,refobjsubid,deptype FROM pg_catalog.pg_depend
            WHERE classid='pg_catalog.pg_class'::regclass AND objid=%s AND objsubid=0
            AND refclassid='pg_catalog.pg_class'::regclass AND deptype IN ('a','i')""",(oid,)).fetchall()
        if len(dependencies)!=1 or dependencies[0][0] not in owners or persistence!='p':
            raise ValueError(f'{schema}.{name}: нужна постоянная последовательность, принадлежащая проверяемому столбцу.')
        owner,position,dependency=dependencies[0]
        if owners[owner][0]!=schema:raise ValueError('Внешняя последовательность не входит в проверяемое состояние.')
        column=connection.execute('SELECT attname,attidentity,atttypid FROM pg_catalog.pg_attribute WHERE attrelid=%s AND attnum=%s AND NOT attisdropped',
            (owner,position)).fetchone()
        config=connection.execute('SELECT seqtypid,seqstart,seqincrement,seqmin,seqmax,seqcache,seqcycle FROM pg_catalog.pg_sequence WHERE seqrelid=%s',(oid,)).fetchone()
        if (column is None or column[2] not in (20,21,23) or config is None or config[0] not in (20,21,23)
                or config[5]!=1 or config[6] or (dependency=='i')!=(column[1] in ('a','d'))):
            raise ValueError(f'{schema}.{name}: поддерживаются целочисленные serial/identity, CACHE 1 и NO CYCLE.')
        uses=connection.execute("""SELECT a.adrelid,a.adnum FROM pg_catalog.pg_depend d
            JOIN pg_catalog.pg_attrdef a ON d.classid='pg_catalog.pg_attrdef'::regclass AND a.oid=d.objid
            WHERE d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=%s""",(oid,)).fetchall()
        if any(tuple(use)!=(owner,position) for use in uses):
            raise ValueError(f'{schema}.{name}: общая последовательность нескольких столбцов/таблиц не поддерживается.')
        if not connection.execute("SELECT pg_catalog.has_sequence_privilege(current_user,%s,'SELECT')",(oid,)).fetchone()[0]:
            raise ValueError(f'{schema}.{name}: для сверки состояния нужно право SELECT на последовательность.')
        result[owner].append(ReplaySequence(schema,name,column[0],oid,owner,position,dependency,tuple(config)))
    return {oid:tuple(sorted(items,key=lambda item:(item.schema,item.name))) for oid,items in result.items()}


def _constant(node):
    if isinstance(node,(exp.Null,exp.Boolean)):return True
    if type(node) is exp.Literal:return True
    if type(node) in (exp.Neg,exp.Paren):return _constant(node.this)
    if type(node) is not exp.Cast or not _constant(node.this):return False
    target=node.args['to']
    if type(target) is not exp.DataType or target.this not in {
            exp.DataType.Type.SMALLINT,exp.DataType.Type.INT,exp.DataType.Type.BIGINT,exp.DataType.Type.DECIMAL,
            exp.DataType.Type.TEXT,exp.DataType.Type.VARCHAR,exp.DataType.Type.BOOLEAN,
            exp.DataType.Type.DATE,exp.DataType.Type.TIMESTAMP,exp.DataType.Type.TIMESTAMPTZ}:return False
    if target.this in {exp.DataType.Type.DATE,exp.DataType.Type.TIMESTAMP,exp.DataType.Type.TIMESTAMPTZ}:
        if isinstance(node.this,exp.Null):return True
        if type(node.this) is not exp.Literal or not node.this.is_string:return False
        try:
            (date.fromisoformat if target.this==exp.DataType.Type.DATE else datetime.fromisoformat)(node.this.this)
        except ValueError:return False
    return True


def _nextval_target(node):
    if type(node) is exp.Dot:
        if type(node.this) is not exp.Identifier or identifier_key(node.this)!='pg_catalog':return None
        node=node.expression
    if type(node) is not exp.Anonymous or node.name.lower()!='nextval' or len(node.expressions)!=1:return None
    argument=node.expressions[0]
    if (type(argument) is not exp.Cast or type(argument.this) is not exp.Literal or not argument.this.is_string
            or argument.args['to'].sql(dialect='postgres').lower() not in {'regclass','pg_catalog.regclass'}):return None
    return argument.this.this


def validate_defaults(connection, table_oid, schema, table, columns, program, sequences, *, source_columns=None):
    active=used_defaults(program,schema,table,tuple(column.name for column in columns),source_columns=source_columns)
    owned={seq.column:seq for seq in sequences}
    signatures=[]
    for column in columns:
        if column.identity and (column.identity not in ('a','d') or column.name not in owned or owned[column.name].dependency!='i'):
            raise ValueError(f'{schema}.{table}.{column.name}: не найдена собственная identity-последовательность.')
        if column.definition_oid or column.identity:
            signatures.append((column.name,column.identity,sha256((column.definition_tree or '').encode()).hexdigest()))
        if column.name not in active or column.definition_oid is None:continue
        # Never deparse a custom-type constant (its output function could run).
        custom=connection.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_depend d
            LEFT JOIN pg_catalog.pg_proc p ON d.refclassid='pg_catalog.pg_proc'::regclass AND p.oid=d.refobjid
            LEFT JOIN pg_catalog.pg_type t ON d.refclassid='pg_catalog.pg_type'::regclass AND t.oid=d.refobjid
            LEFT JOIN pg_catalog.pg_operator o ON d.refclassid='pg_catalog.pg_operator'::regclass AND o.oid=d.refobjid
            LEFT JOIN pg_catalog.pg_collation c ON d.refclassid='pg_catalog.pg_collation'::regclass AND c.oid=d.refobjid
            JOIN pg_catalog.pg_namespace n ON n.oid=COALESCE(p.pronamespace,t.typnamespace,o.oprnamespace,c.collnamespace)
            WHERE d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=%s AND n.nspname<>'pg_catalog')""",
            (column.definition_oid,)).fetchone()[0]
        if custom:raise ValueError(f'{schema}.{table}.{column.name}: вызываемый DEFAULT зависит от пользовательской функции/типа.')
        expression=connection.execute('SELECT pg_catalog.pg_get_expr(adbin,adrelid) FROM pg_catalog.pg_attrdef WHERE oid=%s',
            (column.definition_oid,)).fetchone()
        if expression is None or len(expression[0])>4096:raise ValueError('DEFAULT не найден или превышает лимит 4096 символов.')
        try:node=parse_one(expression[0],read='postgres')
        except Exception:raise ValueError('Не удалось проверить выражение DEFAULT; ничего не вычислялось.') from None
        relation=_nextval_target(node)
        if relation is not None:
            target=connection.execute('SELECT pg_catalog.to_regclass(%s)::oid',(relation,)).fetchone()[0]
            if column.name not in owned or owned[column.name].oid!=target:
                raise ValueError(f'{schema}.{table}.{column.name}: DEFAULT nextval должен использовать собственную последовательность столбца.')
        elif not _constant(node):
            raise ValueError(f'{schema}.{table}.{column.name}: вызываемый DEFAULT должен быть константой или собственным nextval; функции времени/случайности не воспроизводятся.')
    return tuple(signatures)
