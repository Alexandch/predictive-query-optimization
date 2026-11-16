"""Closed ON CONFLICT grammar and catalog-only arbiter validation."""
from sqlglot import exp, parse_one


def identifier_key(node):
    return node.name if node.args.get('quoted') else node.name.lower()


def conflict_assignments(tree):
    conflict=tree.args.get('conflict') if isinstance(tree,exp.Insert) else None
    return tuple(conflict.expressions) if conflict is not None else ()


def _plain_column(node):
    return (type(node) is exp.Column and type(node.this) is exp.Identifier
        and not any(node.args.get(key) for key in ('table','db','catalog')))


def validate_upsert(tree):
    """Allow a single target, scalar assignments, target/EXCLUDED references.

    General expression safety is still enforced by the replay node allowlist.
    Partial/expression arbiters, tuple updates and extra dialect options stay out.
    """
    conflict=tree.args.get('conflict') if isinstance(tree,exp.Insert) else None
    if conflict is None:return None
    if type(conflict) is not exp.OnConflict or any(value and key not in {
            'action','expressions','conflict_keys','constraint','where'} for key,value in conflict.args.items()):
        raise ValueError('ON CONFLICT: частичные/вычисляемые цели и дополнительные параметры не поддерживаются.')
    action=conflict.args.get('action')
    if type(action) is not exp.Var or action.this not in {'DO NOTHING','DO UPDATE'}:
        raise ValueError('ON CONFLICT: нужны DO NOTHING или DO UPDATE.')
    keys=conflict.args.get('conflict_keys') or []
    names=[]
    for key in keys:
        if (type(key) is not exp.Ordered or key.args.get('desc') is not None or key.args.get('nulls_first')
                or any(value and field not in {'this','nulls_first'} for field,value in key.args.items())
                or not _plain_column(key.this)):
            raise ValueError('ON CONFLICT: цель — только список простых столбцов без выражений/сортировки/collation/opclass.')
        names.append(identifier_key(key.this.this))
    constraint=conflict.args.get('constraint')
    if constraint is not None and type(constraint) is not exp.Identifier:
        raise ValueError('ON CONSTRAINT требует простое имя ограничения.')
    if len(names)>8 or len(set(names))!=len(names) or names and constraint is not None:
        raise ValueError('ON CONFLICT: нужны 1–8 различных столбцов либо одно ON CONSTRAINT.')
    assignments=conflict_assignments(tree)
    if action.this=='DO NOTHING':
        if assignments or conflict.args.get('where'):
            raise ValueError('DO NOTHING не содержит SET или WHERE.')
    else:
        if not names and constraint is None:
            raise ValueError('DO UPDATE требует явную цель конфликта.')
        if not assignments or any(type(item) is not exp.EQ or not _plain_column(item.this) for item in assignments):
            raise ValueError('DO UPDATE: только отдельные присваивания неуточнённым столбцам, без кортежей.')
        columns=[identifier_key(item.this.this) for item in assignments]
        if len(set(columns))!=len(columns):raise ValueError('DO UPDATE: столбцы SET не должны повторяться.')
    target=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
    alias=target.args.get('alias')
    # SQLGlot stores INSERT's column list under TableAlias.columns when AS is
    # present. PostgreSQL treats this as insert columns, not alias renaming.
    if alias is not None and (type(alias.this) is not exp.Identifier
            or any(type(column) is not exp.Identifier for column in alias.args.get('columns') or [])):
        raise ValueError('INSERT: нужен простой псевдоним и имена столбцов.')
    name=identifier_key(alias.this if alias is not None else target.this)
    if name=='excluded':raise ValueError('Таблице excluded нужен другой псевдоним: имя EXCLUDED зарезервировано в ON CONFLICT.')
    for expression in (*[item.expression for item in assignments],conflict.args.get('where')):
        if expression is None:continue
        for column in expression.find_all(exp.Column):
            if (column.db or column.catalog or type(column.this) is not exp.Identifier
                    or column.table and identifier_key(column.args['table']) not in {name,'excluded'}):
                raise ValueError('DO UPDATE: доступны только столбцы целевой таблицы/её псевдонима и EXCLUDED.')
    return action


def validate_arbiters(connection,table_oid,schema,table,columns,program):
    """Do not guess which unique object arbitrates; never execute an INSERT."""
    indexes=None
    for command in program.commands:
        if command.kind!='insert':continue
        tree=parse_one(command.sql,read='postgres')
        target=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
        conflict=tree.args.get('conflict')
        if (target.db,target.name)!=(schema,table) or conflict is None:continue
        names=[identifier_key(key.this.this) for key in conflict.args.get('conflict_keys') or []]
        updates=[identifier_key(item.this.this) for item in conflict_assignments(tree)]
        if not set((*names,*updates))<=set(columns):
            raise ValueError(f'{schema}.{table}: неизвестный столбец цели конфликта или SET.')
        constraint=conflict.args.get('constraint')
        if constraint is not None:
            match=connection.execute("""SELECT contype,condeferrable,convalidated FROM pg_catalog.pg_constraint
                WHERE conrelid=%s AND conname=%s""",(table_oid,identifier_key(constraint))).fetchall()
            if len(match)!=1 or match[0][0] not in ('p','u') or match[0][1] or not match[0][2]:
                raise ValueError(f'{schema}.{table}: ON CONSTRAINT требует существующее немедленное PRIMARY KEY/UNIQUE.')
            continue
        if indexes is None:
            indexes=connection.execute("""SELECT i.indimmediate,ARRAY(SELECT a.attname FROM unnest(i.indkey)
                WITH ORDINALITY k(attnum,n) JOIN pg_catalog.pg_attribute a
                ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE k.n<=i.indnkeyatts ORDER BY k.n)
                FROM pg_catalog.pg_index i WHERE i.indrelid=%s AND i.indisunique AND i.indisvalid AND i.indisready
                AND i.indpred IS NULL AND i.indexprs IS NULL""",(table_oid,)).fetchall()
        matches=[index for index in indexes if not names or len(index[1])==len(names) and set(index[1])==set(names)]
        if names and not matches or any(not immediate for immediate,_ in matches):
            raise ValueError(f'{schema}.{table}: нужен подходящий немедленный полный UNIQUE/PRIMARY KEY; отложенные арбитры исключены.')
