"""Row security checks for replay under an explicit role on a laboratory server."""
from psycopg import sql
from sqlglot import exp, parse

from .transaction_context import parse_transaction_control


_POLICY_NODES = {
    exp.Column, exp.Identifier, exp.Literal, exp.Boolean, exp.Null, exp.CurrentUser,
    exp.Paren, exp.And, exp.Or, exp.Not, exp.Is, exp.EQ, exp.NEQ, exp.LT, exp.LTE,
    exp.GT, exp.GTE, exp.In, exp.Between, exp.Cast, exp.DataType,
}


def execution_roles(program):
    return tuple(dict.fromkeys(parse_transaction_control(command.sql).role
        for command in program.commands if command.kind == 'set_role'))


def validate_replay_roles(connection, program, rls_relations):
    roles = execution_roles(program)
    if rls_relations and not roles:
        raise ValueError('Для RLS укажите SET LOCAL ROLE и отдельную роль выполнения на лабораторном сервере.')
    if not roles:
        return
    if rls_relations:
        _validate_policies(connection, rls_relations)
    for role in roles:
        flags = connection.execute('''SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles
            WHERE rolname=%s''', (role,)).fetchone()
        if flags is None or any(flags):
            raise ValueError('Роль выполнения должна существовать и не иметь SUPERUSER или BYPASSRLS.')
        with connection.transaction(force_rollback=True):
            connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            if connection.execute('SELECT current_user').fetchone()[0] != role:
                raise ValueError('PostgreSQL не переключился на указанную роль выполнения.')
            if rls_relations and not connection.execute('''SELECT bool_and(pg_catalog.row_security_active(oid))
                FROM unnest(%s::oid[]) o(oid)''', (rls_relations,)).fetchone()[0]:
                raise ValueError('RLS должна действовать для роли выполнения на всех проверяемых RLS-таблицах.')


def _validate_policies(connection, relation_oids):
    if connection.execute('''SELECT bool_or(pg_catalog.row_security_active(oid))
        FROM unnest(%s::oid[]) o(oid)''', (relation_oids,)).fetchone()[0]:
        raise ValueError('Для сравнения снимков лабораторное подключение должно видеть все строки RLS-таблиц.')
    rows = connection.execute('''SELECT p.polname, pg_catalog.pg_get_expr(p.polqual, p.polrelid),
        pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), EXISTS (
            SELECT 1 FROM pg_catalog.pg_depend d
            WHERE d.classid='pg_catalog.pg_policy'::regclass AND d.objid=p.oid
              AND (d.refclassid<>'pg_catalog.pg_class'::regclass OR d.refobjid<>p.polrelid)
        ) FROM pg_catalog.pg_policy p WHERE p.polrelid=ANY(%s)''', (relation_oids,)).fetchall()
    for name, using, check, foreign_dependency in rows:
        if foreign_dependency or any(expr is not None and not _simple_policy(expr) for expr in (using, check)):
            raise ValueError(f'Политика {name}: поддерживаются условия по столбцам, константам и CURRENT_USER.')


def _simple_policy(text):
    statements = parse(text, read='postgres')
    if len(statements) != 1 or statements[0] is None:
        return False
    for node in statements[0].walk():
        if type(node) not in _POLICY_NODES:
            return False
        if isinstance(node, exp.Column) and (node.table or node.db or node.catalog):
            return False
        if isinstance(node, exp.DataType) and node.this not in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR, exp.DataType.Type.NAME):
            return False
    return True
