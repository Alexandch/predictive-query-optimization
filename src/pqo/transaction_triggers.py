"""Catalog checks for deterministic row audit triggers in disposable replays."""
import re


_NAME = r"[a-z_][a-z_0-9]{0,62}"
_AUDIT_BODY = re.compile(
    rf"\s*BEGIN\s+INSERT\s+INTO\s+({_NAME})\.({_NAME})\s*"
    rf"\(([^()]{{1,1024}})\)\s+VALUES\s*\(([^()]{{1,1024}})\)\s*;\s*"
    r"RETURN\s+NULL\s*;\s*END\s*;?\s*", re.IGNORECASE | re.ASCII,
)
_ROW_VALUE = re.compile(rf"(NEW|OLD)\.({_NAME})", re.IGNORECASE | re.ASCII)
_EVENT_ROWS = {5: {'new'}, 9: {'old'}, 17: {'new', 'old'}}


def validate_replay_triggers(connection, relations, columns):
    """Permit one direct audit INSERT per AFTER ROW invocation, without chained audit triggers."""
    triggers = connection.execute("""SELECT t.tgrelid, t.tgname, t.tgtype, t.tgenabled,
        t.tgisinternal, c.contype, n.nspname, p.proname, pg_catalog.cardinality(t.tgattr::smallint[]),
        t.tgnargs, t.tgqual IS NOT NULL, t.tgconstraint, t.tgoldtable, t.tgnewtable,
        l.lanname, p.prosrc, p.prosecdef, p.proconfig, p.pronargs, p.prosupport::oid
        FROM pg_catalog.pg_trigger t
        LEFT JOIN pg_catalog.pg_constraint c ON c.oid=t.tgconstraint
        JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_language l ON l.oid=p.prolang
        WHERE t.tgrelid=ANY(%s) AND t.tgenabled<>'D'""", (sorted(relations),)).fetchall()
    user_tables = {relations[row[0]] for row in triggers if not row[4]}
    for (oid, name, trigger_type, enabled, internal, constraint, namespace, function,
         update_columns, args, condition, constraint_oid, old_table, new_table,
         language, body, security_definer, config, function_args, support) in triggers:
        source = relations[oid]
        if internal:
            if constraint == 'f' and namespace == 'pg_catalog' and function in {
                'RI_FKey_check_ins', 'RI_FKey_check_upd', 'RI_FKey_noaction_del',
                'RI_FKey_noaction_upd', 'RI_FKey_restrict_del', 'RI_FKey_restrict_upd',
            }:
                continue
            raise ValueError(f'{source[0]}.{source[1]}: неизвестный внутренний триггер {name}.')
        allowed_rows = _EVENT_ROWS.get(trigger_type)
        refusal = f'{source[0]}.{source[1]}: триггер {name} вне поддерживаемого контракта аудита.'
        if (allowed_rows is None or enabled != 'O' or update_columns or args or condition or constraint_oid
                or old_table or new_table or language != 'plpgsql' or security_definer
                or config is not None or function_args or support):
            raise ValueError(refusal)
        match = _AUDIT_BODY.fullmatch(body)
        if not match:
            raise ValueError(refusal)
        schema, table, insert_columns, values = match.groups()
        target = (schema.lower(), table.lower())
        names = [name.strip().lower() for name in insert_columns.split(',')]
        values = [value.strip() for value in values.split(',')]
        if (target not in columns or target in user_tables or len(names) != len(values)
                or len(names) != len(set(names)) or set(names) != set(columns[target])
                or not all(re.fullmatch(_NAME, name, re.ASCII) for name in names)):
            raise ValueError(refusal)
        for value in values:
            row_value = _ROW_VALUE.fullmatch(value)
            if row_value and row_value[1].lower() in allowed_rows and row_value[2].lower() in columns[source]:
                continue
            if re.fullmatch(r"'[A-Za-z0-9_ -]{0,63}'", value, re.ASCII):
                continue
            raise ValueError(refusal)
