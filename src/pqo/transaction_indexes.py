"""Read-only workload index hypotheses. No DDL, replay or performance promises."""
from __future__ import annotations

from dataclasses import dataclass
import time

import psycopg
from sqlglot import exp, parse_one

from .analysis_control import AnalysisControl
from .config import DatabaseSettings
from .index_actions import IndexAction
from .transaction_inspection import TransactionInspection, inspect_transaction
from .transaction_planning import TransactionPlanReport, planning_restriction
from .transaction_parameters import is_typed_constant


@dataclass(frozen=True)
class ExistingIndex:
    name: str
    keys: tuple[str | None, ...]
    includes: tuple[str | None, ...] = ()
    compatible: bool = True


@dataclass(frozen=True)
class RelationIndexes:
    schema: str
    table: str
    columns: tuple[str, ...]
    indexes: tuple[ExistingIndex, ...] = ()
    estimated_rows: float = 0
    size_bytes: int = 0
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    hot_updates: int = 0
    write_metadata_known: bool = False
    relation_kind: str = "unknown"
    generated_columns: tuple[str, ...] = ()
    has_user_triggers: bool = False
    has_rules: bool = False
    has_descendants: bool = False
    fk_update_writes: bool = False
    fk_delete_writes: bool = False


@dataclass(frozen=True)
class IndexUse:
    statement_number: int
    reason: str
    observed_scan_nodes: tuple[str, ...] = ()
    statement_kind: str = "select"
    changes_data: bool = False


@dataclass(frozen=True)
class IndexWriteImpact:
    statement_number: int
    kind: str
    changed_index_columns: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class TransactionIndexCandidate:
    action: IndexAction
    uses: tuple[IndexUse, ...]
    writes: tuple[IndexWriteImpact, ...]
    covered_by: tuple[str, ...]
    key_covered_by: tuple[str, ...]
    relation: RelationIndexes

    @property
    def status(self):
        return "covered" if self.covered_by else "covering_review" if self.key_covered_by else "candidate"

    @property
    def read_uses(self):
        return tuple(use for use in self.uses if not use.changes_data)

    @property
    def write_lookup_uses(self):
        return tuple(use for use in self.uses if use.changes_data)


@dataclass(frozen=True)
class TransactionIndexReport:
    database_name: str
    candidates: tuple[TransactionIndexCandidate, ...]
    notes: tuple[str, ...]
    truncated: bool = False
    relations: tuple[RelationIndexes, ...] = ()


@dataclass
class _Access:
    number: int
    relation: tuple[str, str]
    equalities: list[str]
    ranges: list[str]
    ordering: list[str]
    projected: list[str]


_CONTEXTS = (exp.Select, exp.Update, exp.Delete, exp.Insert)


def _owner(node):
    return node.find_ancestor(*_CONTEXTS)


def _relation(table):
    return (table.db, table.name) if isinstance(table, exp.Table) and table.db and not table.catalog else None


def _target(node):
    table = node.this
    return _relation(table.this if isinstance(table, exp.Schema) else table)


def _terms(expression):
    if isinstance(expression, exp.And):
        yield from _terms(expression.this)
        yield from _terms(expression.expression)
    elif isinstance(expression, exp.Paren):
        yield from _terms(expression.this)
    else:
        yield expression


def _accesses(tree, number, metadata, notes):
    """Bind only direct base-table columns in their own SQL scope.

    No CTE/subquery lineage inference or predicate movement. Unknown derived
    sources prevent resolving unqualified columns. Never merge nested aliases.
    """
    result = []
    for context in tree.walk():
        if not isinstance(context, (exp.Select, exp.Update, exp.Delete)):
            continue
        tables = [table for table in context.find_all(exp.Table) if _owner(table) is context]
        bindings = {table.alias_or_name: _relation(table) for table in tables}
        unknown_source = any(relation not in metadata for relation in bindings.values())
        unknown_source |= any(_owner(subquery) is context for subquery in context.find_all(exp.Subquery))
        accesses = {alias: _Access(number, relation, [], [], [], [])
                    for alias, relation in bindings.items() if relation in metadata}
        aliases = {item.alias: item.this for item in context.expressions if isinstance(item, exp.Alias)}

        def resolve(column, order=False):
            if not isinstance(column, exp.Column) or _owner(column) is not context:
                return None
            if order and not column.table and column.name in aliases:
                column = aliases[column.name]
                if not isinstance(column, exp.Column):
                    return None
            if column.table:
                access = accesses.get(column.table)
                if column.db and (access is None or column.db != access.relation[0]):
                    return None
                if column.catalog:
                    return None
                return access if access and column.name in metadata[access.relation].columns else None
            if unknown_source:
                return None
            matches = [item for item in accesses.values() if column.name in metadata[item.relation].columns]
            if len(matches) != 1:
                if len(matches) > 1:
                    notes.add(f"Команда {number}: неоднозначный столбец {column.name}; кандидат по нему не строился.")
                return None
            return matches[0]

        def add(column, field, order=False):
            access = resolve(column, order)
            if access is None:
                return
            actual = aliases.get(column.name, column) if order and not column.table else column
            name = actual.name
            values = getattr(access, field)
            if name not in values:
                values.append(name)

        predicates = []
        where = context.args.get("where")
        if where is not None:
            predicates.append(where.this)
        for join in context.find_all(exp.Join):
            if _owner(join) is context and join.args.get("on") is not None:
                predicates.append(join.args["on"])
        simple_value = (exp.Column, exp.Literal, exp.Boolean, exp.Null)
        def direct_value(node):
            return isinstance(node, simple_value) or is_typed_constant(node)
        for predicate in predicates:
            for term in _terms(predicate):
                if isinstance(term, exp.Or):
                    notes.add(f"Команда {number}: ветви OR не объединялись в составной индекс.")
                    continue
                if isinstance(term, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                    if direct_value(term.this) and direct_value(term.expression):
                        field = "equalities" if isinstance(term, exp.EQ) else "ranges"
                        add(term.this, field)
                        add(term.expression, field)
                elif isinstance(term, exp.Between):
                    if all(direct_value(term.args.get(key)) for key in ("low", "high")):
                        add(term.this, "ranges")
                elif isinstance(term, exp.In) and term.expressions and all(isinstance(x, exp.Literal) or is_typed_constant(x) for x in term.expressions):
                    add(term.this, "ranges")
        order = context.args.get("order")
        if order:
            for item in order.expressions:
                add(item.this, "ordering", order=True)
        if isinstance(context, exp.Select):
            for expression in context.expressions:
                for column in expression.find_all(exp.Column):
                    add(column, "projected")
        result.extend(accesses.values())
    return result


def _lookup_reason(access, action):
    if access.relation != (action.schema_name, action.table_name):
        return None
    first = action.key_columns[0]
    if first in (*access.equalities, *access.ranges):
        return f"Ведущий ключ {first} встречается в прямом условии фильтра/соединения."
    if access.ordering and first == access.ordering[0]:
        return f"Ведущий ключ {first} встречается первым в ORDER BY."
    return None


def read_statements_for_index(inspection, action, metadata):
    """Conservative SELECT relevance using the same scoped bindings as candidates.

    A projection/table reference alone is not a lookup. Modifying CTEs are
    excluded as whole statements, even if their outer command is SELECT.
    This is structural eligibility, not proof that PostgreSQL uses the index.
    """
    numbers = []
    for item in inspection.statements:
        if item.changes_data:
            continue
        tree = parse_one(item.sql, read="postgres")
        if isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)) and any(
            _lookup_reason(access, action) for access in _accesses(tree, item.number, metadata, set())
        ):
            numbers.append(item.number)
    return tuple(numbers)


def _writes(trees):
    result = []
    for number, tree in trees.items():
        for node in tree.walk():
            if not isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                continue
            relation = _target(node)
            if relation is None:
                continue
            columns = set()
            unknown = False
            if isinstance(node, exp.Update):
                for assignment in node.expressions:
                    lhs = assignment.this if isinstance(assignment, exp.EQ) else None
                    targets = lhs.expressions if isinstance(lhs, exp.Tuple) else [lhs]
                    if not targets or any(not isinstance(item, exp.Column) for item in targets):
                        unknown = True
                    else:
                        columns.update(item.name for item in targets)
            result.append((number, type(node).__name__.lower(), relation, None if unknown else columns))
    return result


def _scan_nodes(plan, relation, relations):
    # FORMAT JSON without VERBOSE can omit Schema. Do not bind duplicate table
    # names in different schemas to the same scan accidentally.
    ambiguous = sum(item[1] == relation[1] for item in relations) > 1
    nodes = set()
    def visit(node):
        if isinstance(node, dict):
            if node.get("Relation Name") == relation[1] and (
                node.get("Schema") == relation[0] or not node.get("Schema") and not ambiguous
            ) and "Scan" in node.get("Node Type", ""):
                nodes.add(node["Node Type"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(plan)
    return tuple(sorted(nodes))


def build_transaction_index_report(inspection: TransactionInspection, plans, metadata, *, eligible=None, extra_notes=()):
    """Pure structural assessment; the sort order is not a learned benefit score."""
    eligible = set(eligible if eligible is not None else (p.number for p in plans.statements if p.status == "planned"))
    trees = {item.number: parse_one(item.sql, read="postgres") for item in inspection.statements}
    notes = set(extra_notes)
    accesses = [access for number, tree in trees.items() if number in eligible
                for access in _accesses(tree, number, metadata, notes)]
    actions = set()
    truncated = False
    for access in accesses:
        keys = [(name,) for name in (*access.equalities, *access.ranges)]
        for names in ((*access.equalities, *access.ranges, *access.ordering),
                      (*access.equalities, *access.ordering, *access.ranges)):
            combined = tuple(dict.fromkeys(names))[:3]
            if combined:
                keys.append(combined)
        for key in keys:
            includes = tuple(name for name in access.projected if name not in key)[:3]
            for payload in ((), includes) if includes else ((),):
                try:
                    action = IndexAction.create(*access.relation, key, payload)
                except ValueError:
                    notes.add("Некоторые идентификаторы не поддерживаются форматом индексных кандидатов.")
                    continue
                if len(actions) >= 128 and action not in actions:
                    truncated = True
                else:
                    actions.add(action)
    writes = _writes(trees)
    candidates = []
    plan_by_number = {plan.number: plan for plan in plans.statements}
    statement_by_number = {item.number: item for item in inspection.statements}
    for action in actions:
        relation_key = (action.schema_name, action.table_name)
        relation = metadata[relation_key]
        uses = {}
        for access in accesses:
            reason = _lookup_reason(access, action)
            if reason is None:
                continue
            plan = plan_by_number.get(access.number)
            statement = statement_by_number[access.number]
            uses[access.number] = IndexUse(access.number, reason,
                _scan_nodes(plan.plan_json, relation_key, metadata) if plan else (),
                statement.kind, statement.changes_data)
        key_covered = tuple(sorted(index.name for index in relation.indexes if index.compatible
            and index.keys[:len(action.key_columns)] == action.key_columns))
        covered = tuple(sorted(index.name for index in relation.indexes if index.name in key_covered
            and set(action.include_columns) <= set((*index.keys, *index.includes))))
        impacts = []
        for number, kind, target, columns in writes:
            if target != relation_key:
                continue
            overlap = tuple(sorted(set((*action.key_columns, *action.include_columns)) & columns)) if columns is not None else ()
            if kind == "update" and columns is not None:
                message = (f"UPDATE меняет столбцы ключа/INCLUDE: {', '.join(overlap)}; риск дополнительного обслуживания и потери HOT."
                    if overlap else "UPDATE не меняет ключ/INCLUDE этого кандидата; нулевые издержки не гарантированы (HOT зависит и от других условий).")
            elif kind == "delete":
                message = "DELETE создаёт мёртвые версии; дополнительный индекс увеличивает последующее обслуживание/очистку."
            else:
                message = "Запись требует обслуживания дополнительного индекса; величина замедления не измерялась."
            impacts.append(IndexWriteImpact(number, kind, overlap, message))
        candidates.append(TransactionIndexCandidate(action, tuple(uses[key] for key in sorted(uses)),
            tuple(impacts), covered, key_covered, relation))
    candidates.sort(key=lambda item: (
        {"candidate": 0, "covering_review": 1, "covered": 2}[item.status],
        -len(item.uses), sum(bool(write.changed_index_columns) for write in item.writes),
        len(item.action.include_columns), len(item.action.key_columns),
        item.action.schema_name, item.action.table_name, item.action.key_columns, item.action.include_columns))
    if len(eligible) != len(inspection.statements):
        notes.add("Не все команды имеют допустимые планы; список кандидатов и оценка влияния могут быть неполными.")
    notes.add("Это структурные кандидаты, не доказанное ускорение. Направления сортировки/NULLS и селективность требуют проверки планом с индексом.")
    notes.add("Издержки записи качественные: частоты, изменённые строки, генерируемые столбцы, триггеры, каскады и конкурентная нагрузка не воспроизводились.")
    return TransactionIndexReport(plans.database_name, tuple(candidates), tuple(sorted(notes)), truncated,
        tuple(metadata[key] for key in sorted(metadata)))


def _load_relation_indexes(connection, schema, table):
    row = connection.execute("""
        SELECT c.oid, greatest(c.reltuples,0)::float8, pg_catalog.pg_total_relation_size(c.oid),
               coalesce(s.n_tup_ins,0), coalesce(s.n_tup_upd,0), coalesce(s.n_tup_del,0), coalesce(s.n_tup_hot_upd,0),
               ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum),
               c.relkind,
               ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped AND a.attgenerated<>'' ORDER BY a.attnum),
               EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal AND t.tgenabled<>'D'),
               c.relhasrules, c.relhassubclass,
               EXISTS(SELECT 1 FROM pg_catalog.pg_constraint f WHERE f.contype='f' AND f.confrelid=c.oid AND f.confupdtype IN ('c','n','d')),
               EXISTS(SELECT 1 FROM pg_catalog.pg_constraint f WHERE f.contype='f' AND f.confrelid=c.oid AND f.confdeltype IN ('c','n','d'))
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid=c.oid
        WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p')
        """, (schema, table)).fetchone()
    if row is None:
        return None
    indexes = connection.execute("""
        SELECT ic.relname, i.indnkeyatts,
               ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
                     LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.ord),
               (i.indisvalid AND i.indisready AND i.indislive AND NOT i.indcheckxmin AND am.amname='btree'
                AND i.indpred IS NULL AND i.indexprs IS NULL
                AND NOT EXISTS (SELECT 1 FROM unnest(i.indoption) opt WHERE opt<>0)
                AND NOT EXISTS (SELECT 1 FROM unnest(i.indclass) op(opcoid)
                                JOIN pg_catalog.pg_opclass opc ON opc.oid=op.opcoid WHERE NOT opc.opcdefault)
                AND NOT EXISTS (SELECT 1 FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
                                JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                                WHERE k.ord<=i.indnkeyatts AND i.indcollation[(k.ord-1)::int]<>a.attcollation))
        FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_catalog.pg_am am ON am.oid=ic.relam WHERE i.indrelid=%s
        """, (row[0],)).fetchall()
    extra = dict(write_metadata_known=True, relation_kind=row[8], generated_columns=tuple(row[9]),
        has_user_triggers=bool(row[10]), has_rules=bool(row[11]), has_descendants=bool(row[12]),
        fk_update_writes=bool(row[13]), fk_delete_writes=bool(row[14])) if len(row) >= 15 else {}
    return RelationIndexes(schema, table, tuple(row[7]), tuple(
        ExistingIndex(name, tuple(columns[:count]), tuple(columns[count:]), bool(compatible))
        for name, count, columns, compatible in indexes), float(row[1]), *map(int, row[2:7]), **extra)


def collect_transaction_index_candidates(sql_text, settings: DatabaseSettings, *, plans: TransactionPlanReport, control=None):
    """Read system catalogs only. Input transaction SQL is never sent to execute()."""
    control = control or AnalysisControl()
    control.check()
    inspection = inspect_transaction(sql_text)
    if inspection != plans.inspection or settings.dbname != plans.database_name:
        raise ValueError("Оценочные планы не соответствуют текущему SQL/базе. Получите планы заново.")
    eligible = {item.number for item in inspection.statements
                if any(plan.number == item.number and plan.status == "planned" for plan in plans.statements)
                and planning_restriction(item, settings) is None}
    relations = set()
    for statement in inspection.statements:
        tree = parse_one(statement.sql, read="postgres")
        relations.update(relation for table in tree.find_all(exp.Table)
            if (relation := _relation(table)) and relation[0] in settings.allowed_schemas)
    metadata, notes = {}, []
    if not eligible:
        notes.append("Нет команд с допустимыми оценочными планами; сначала получите планы.")
    elif relations:
        deadline = time.monotonic() + 30
        if len(relations) > 64:
            notes.append("Метаданные ограничены первыми 64 таблицами; список кандидатов неполный.")
        with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
            with control.monitor(connection), connection.transaction(force_rollback=True):
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute("SET LOCAL search_path = pg_catalog")
                connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(min(30_000, max(1, settings.statement_timeout_ms))),))
                connection.execute("SET LOCAL lock_timeout = '1s'")
                for schema, table in sorted(relations)[:64]:
                    control.report(f"Индексные кандидаты: метаданные {schema}.{table}; без создания индексов…")
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if remaining_ms < 2:
                        notes.append("Бюджет чтения метаданных 30 с исчерпан; список кандидатов неполный.")
                        break
                    try:
                        with connection.transaction(force_rollback=True):
                            connection.execute("SELECT set_config('statement_timeout', %s, true)",
                                (str(min(max(1, settings.statement_timeout_ms), 10_000, remaining_ms // 2)),))
                            relation = _load_relation_indexes(connection, schema, table)
                        if relation is not None:
                            metadata[schema, table] = relation
                        else:
                            notes.append(f"{schema}.{table}: таблица недоступна или это не обычная/секционированная таблица.")
                    except psycopg.Error as exc:
                        control.check()
                        notes.append(f"{schema}.{table}: метаданные не получены (PostgreSQL {exc.sqlstate}).")
    control.check()
    result = build_transaction_index_report(inspection, plans, metadata, eligible=eligible, extra_notes=notes)
    control.check()
    return result
