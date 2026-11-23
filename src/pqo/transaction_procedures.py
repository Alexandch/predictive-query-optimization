"""Catalog proof for a small class of already defined SQL procedures."""
import re

from sqlglot import exp, parse_one

from .transaction_context import parse_replay_call, split_transaction_sql


_ATOMIC_BODY = re.compile(r'^BEGIN ATOMIC\s*\n(.*)\nEND\s*;?\s*$', re.M | re.S)


def procedure_writes(connection, settings, program):
    from .transaction_replay_guard import prepare_replay

    prepared = {}
    for command in program.commands:
        if command.kind != 'call' or command.sql in prepared:
            continue
        schema, name = parse_replay_call(command.sql)
        message = f'Процедура {schema}.{name}: нужен единственный SQL CALL без аргументов и одна команда записи в BEGIN ATOMIC.'
        rows = connection.execute('''SELECT p.prokind, l.lanname, p.pronargs, p.proallargtypes IS NULL,
            p.prosecdef, p.proconfig IS NULL, p.prosqlbody IS NOT NULL,
            CASE WHEN p.prokind='p' THEN pg_catalog.pg_get_functiondef(p.oid) END, p.oid, n.oid
            FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_language l ON l.oid=p.prolang
            WHERE n.nspname=%s AND p.proname=%s''', (schema, name)).fetchall()
        if len(rows) != 1 or rows[0][:7] != ('p', 'sql', 0, True, False, True, True):
            raise ValueError(message)
        definition, procedure_oid, namespace_oid = rows[0][7:]
        body = _ATOMIC_BODY.search(definition)
        parts = split_transaction_sql(body[1]) if body else ()
        if len(parts) != 1:
            raise ValueError(message)
        tree = parse_one(parts[0], read='postgres')
        if (type(tree) not in {exp.Insert, exp.Update, exp.Delete} or tree.args.get('returning')
                or tree.find(exp.Join, exp.Select, exp.Subquery, exp.With) is not None):
            raise ValueError(message)
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if type(target) is not exp.Table:
            raise ValueError(message)
        virtual = f'BEGIN; {parts[0]}; SELECT COUNT(*) FROM {target.sql(dialect="postgres")}; COMMIT;'
        guarded = prepare_replay(virtual, settings)
        if len(guarded.tables) != 1:
            raise ValueError(message)
        target_schema, target_name = guarded.tables[0]
        foreign_dependency = connection.execute('''SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_depend d
            WHERE d.classid='pg_catalog.pg_proc'::regclass AND d.objid=%s
              AND NOT ((d.refclassid='pg_catalog.pg_namespace'::regclass AND d.refobjid=%s)
                OR (d.refclassid='pg_catalog.pg_class'::regclass AND EXISTS (
                    SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE c.oid=d.refobjid AND n.nspname=%s AND c.relname=%s)))
            )''', (procedure_oid, namespace_oid, target_schema, target_name)).fetchone()[0]
        if foreign_dependency:
            raise ValueError('Процедура зависит от объектов вне проверяемой таблицы и встроенных операций.')
        prepared[command.sql] = (guarded.commands[0], (target_schema, target_name))
    return tuple(prepared.values())


def validate_procedure_keys(writes, tables):
    keys = {(t.schema, t.name): set(t.primary_key) for t in tables}
    for command, target in writes:
        if command.kind == 'insert':
            continue
        tree = parse_one(command.sql, read='postgres')
        conditions = [tree.args['where'].this]
        equal_keys = set()
        while conditions:
            node = conditions.pop().unnest()
            if isinstance(node, exp.And):
                conditions.extend((node.this, node.expression))
            elif type(node) is exp.EQ and type(node.this) is exp.Column and type(node.expression) is exp.Literal:
                label = node.this.this
                equal_keys.add(label.name if label.args.get('quoted') else label.name.lower())
        if not keys[target] <= equal_keys:
            raise ValueError('В UPDATE/DELETE внутри CALL нужен фильтр по всем столбцам первичного ключа.')
