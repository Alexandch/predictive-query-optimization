"""Statement-local EXPLAIN resource diagnostics on fresh, owned clones.

Normal executions retain the original result verification. Instrumented runs
execute each command once, discard EXPLAIN's raw plan, and verify final data,
sequences and returned row counts. EXPLAIN never returns the result values.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
import time

import psycopg
from psycopg import sql
from sqlglot import parse_one

from .analysis_control import AnalysisCancelled, AnalysisControl
from .connections import password_failure
from .index_actions import IndexAction, IndexActionKind
from .transaction_comparison import source_context_digest, sql_digest
from .transaction_context import CONTROL_KINDS, read_transaction_options
from .transaction_replay import (
    MAX_DATABASE_BYTES, ScratchDatabases, _build_trial_indexes, _kwargs, _sample,
    _timeout, sequence_digest, state_digest, validate_reference,
)
from .transaction_replay_guard import prepare_replay, replay_catalog
from .transaction_select_rewrite import prepare_select_replacement


COUNTERS = {
    'shared_hit_blocks': 'Shared Hit Blocks', 'shared_read_blocks': 'Shared Read Blocks',
    'shared_dirtied_blocks': 'Shared Dirtied Blocks', 'shared_written_blocks': 'Shared Written Blocks',
    'local_hit_blocks': 'Local Hit Blocks', 'local_read_blocks': 'Local Read Blocks',
    'local_dirtied_blocks': 'Local Dirtied Blocks', 'local_written_blocks': 'Local Written Blocks',
    'temp_read_blocks': 'Temp Read Blocks', 'temp_written_blocks': 'Temp Written Blocks',
    'wal_records': 'WAL Records', 'wal_fpi': 'WAL FPI', 'wal_bytes': 'WAL Bytes',
}


@dataclass(frozen=True)
class PlanResources:
    execution_ms: float | None
    output_rows: int
    counters: dict[str, int | None]
    max_operator_memory_kb: int | None
    max_sort_disk_kb: int | None
    triggers: tuple[tuple[str, str, int, float | None], ...] = ()


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('EXPLAIN вернул некорректный целочисленный счётчик ресурсов.')
    return value


def parse_resources(document):
    if (not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict)
            or not isinstance(document[0].get('Plan'), dict)):
        raise ValueError('Не получен один JSON-план EXPLAIN ANALYZE.')
    root, payload = document[0]['Plan'], document[0]
    rows = _integer(root.get('Actual Rows'))
    if type(root.get('Actual Loops')) is not int or root['Actual Loops'] != 1:
        raise ValueError('Для проверки числа выходных строк нужен один корневой цикл плана.')
    execution = payload.get('Execution Time')
    if execution is not None and (type(execution) not in (int, float) or not math.isfinite(execution) or execution < 0):
        raise ValueError('Некорректное время EXPLAIN ANALYZE.')
    counters = {name: _integer(root[key]) if key in root else None for name, key in COUNTERS.items()}
    # Root counters include child nodes. Never sum them over the plan tree.
    memories, disks, queue, count = [], [], [root], 0
    while queue:
        node = queue.pop()
        count += 1
        if not isinstance(node, dict) or count > 10000:
            raise ValueError('План ресурсов слишком большой или имеет неизвестную структуру.')
        if 'Peak Memory Usage' in node:
            memories.append(_integer(node['Peak Memory Usage']))
        if 'Sort Space Used' in node:
            if node.get('Sort Space Type') == 'Memory':
                memories.append(_integer(node['Sort Space Used']))
            elif node.get('Sort Space Type') == 'Disk':
                disks.append(_integer(node['Sort Space Used']))
        for key in ('Plans', 'Workers'):
            children = node.get(key, [])
            if not isinstance(children, list):
                raise ValueError('Неизвестная структура дочерних узлов EXPLAIN.')
            queue.extend(children)
    triggers = []
    for trigger in payload.get('Triggers', []):
        duration = trigger.get('Time')
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
            raise ValueError('Некорректное время триггера.')
        triggers.append((trigger['Trigger Name'], trigger['Relation'], _integer(trigger['Calls']), duration))
    return PlanResources(execution, rows, counters, max(memories, default=None), max(disks, default=None), tuple(triggers))


@dataclass(frozen=True)
class ResourceCommand:
    number: int
    kind: str
    resources: PlanResources | None
    output_count_checked: bool


@dataclass(frozen=True)
class ProfileSample:
    database: str
    block_size: int
    work_mem_kb: int
    commands: tuple[ResourceCommand, ...]
    counters: dict[str, int | None]
    max_operator_memory_kb: int | None
    max_sort_disk_kb: int | None
    initial_state: tuple
    final_state: tuple
    initial_sequences: tuple
    final_sequences: tuple
    transaction_options: object
    ending: str
    index_size_bytes: int
    index_build_ms: float
    states_match_control: bool
    output_counts_match_control: bool
    result_values_verified: bool = False


@dataclass(frozen=True)
class ResourceVariant:
    kind: str
    control: object
    profile: ProfileSample | None


@dataclass(frozen=True)
class TransactionResources:
    status: str
    reference_database: str
    request_digest: str
    work_mem_kb: int | None
    variants: tuple[ResourceVariant, ...]
    normal_variants_equivalent: bool | None
    profiles_match_controls: bool
    cleanup_verified: bool
    created_databases: tuple[str, ...]
    leftover_databases: tuple[str, ...]
    reasons: tuple[str, ...]
    recovery_manifest: str | None


def prepare_resource_request(text, settings, actions=(), number=None, variant_sql=None, work_mem_kb=None):
    if work_mem_kb is not None and (type(work_mem_kb) is not int or not 64 <= work_mem_kb <= 65536):
        raise ValueError('work_mem: 64–65536 КиБ либо настройка эталона.')
    if number is not None and variant_sql is None:
        raise ValueError('Для замены SELECT нужен SQL варианта.')
    try:
        if variant_sql is not None:
            prepared = prepare_select_replacement(text, settings, number, variant_sql)
            original, variant = prepared.original, prepared.variant
        else:
            original = variant = prepare_replay(text, settings, allow_select_wrapper=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        raise ValueError('Не удалось подготовить SQL для профиля ресурсов; ничего не выполнялось.') from None
    actions = tuple(actions)
    if (len(actions) > 2 or any(not isinstance(a, IndexAction) or a.kind is not IndexActionKind.CREATE
            or (a.schema_name, a.table_name) not in original.tables for a in actions) or len(set(actions)) != len(actions)):
        raise ValueError('Нужны не более двух разных CREATE-кандидатов текущей транзакции.')
    programs = [('original', original, ())]
    if actions or variant_sql is not None:
        programs.append(('combined' if actions and variant_sql is not None else 'indexes' if actions else 'rewrite', variant, actions))
    return tuple(programs)


def resource_request_digest(text, settings, reference, actions=(), number=None, variant_sql=None, work_mem_kb=None):
    programs = prepare_resource_request(text, settings, actions, number, variant_sql, work_mem_kb)
    payload = ('transaction-resources-v1', sql_digest(text), source_context_digest(settings), reference,
        [(kind, [sql_digest(c.sql) for c in program.commands], [asdict(a) for a in indexes]) for kind, program, indexes in programs], work_mem_kb)
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _profile_sample(settings, database, program, actions, normal, work_mem_kb, deadline, control):
    with psycopg.connect(**_kwargs(settings, database), autocommit=True) as c, control.monitor(c):
        for statement in ("SET timezone='UTC'", "SET datestyle='ISO, YMD'", 'SET standard_conforming_strings=on',
                'SET jit=off', 'SET max_parallel_workers_per_gather=0'):
            c.execute(statement)
        c.execute(sql.SQL('SET work_mem={}').format(sql.Literal(f'{work_mem_kb}kB')))
        block_size = int(c.execute('SHOW block_size').fetchone()[0])
        if block_size <= 0:
            raise ValueError('Не получен размер блока PostgreSQL.')
        tables = replay_catalog(c, settings, program)
        audit_triggers = c.execute('''SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class r ON r.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname=ANY(%s) AND NOT t.tgisinternal AND t.tgenabled='O')''',
            (sorted(settings.allowed_schemas),)).fetchone()[0]
        timing = 'ON' if audit_triggers else 'OFF'
        initial = state_digest(c, tables, settings, deadline, control)
        initial_sequences = sequence_digest(c, tables, settings, deadline, control)
        metadata = {(t.schema, t.name): {name for name, _ in t.columns} for t in tables}
        for action in actions:
            if not set((*action.key_columns, *action.include_columns)) <= metadata.get((action.schema_name, action.table_name), set()):
                raise ValueError('Столбцы индекса не найдены в проверенной копии.')
        build_ms, size, _ = _build_trial_indexes(c, actions, settings, deadline, control)
        readings, observed, counts_match = [], None, True
        c.execute(program.options.begin_sql)
        try:
            for command, expected in zip(program.commands, normal.commands, strict=True):
                control.report(f'Профиль ресурсов: {command.number} {command.kind.upper()}; отдельная копия…')
                control.check()
                _timeout(c, deadline, settings)
                if observed is None and command.kind != 'set_transaction':
                    observed = read_transaction_options(c)
                    wanted = program.effective_options
                    if any(v is not None and v != getattr(observed, k) for k, v in vars(wanted).items()):
                        raise RuntimeError('Режимы инструментированного прогона не соответствуют SQL.')
                if command.kind in CONTROL_KINDS:
                    c.execute(command.sql)
                    readings.append(ResourceCommand(command.number, command.kind, None, False))
                else:
                    raw = c.execute(f'EXPLAIN (ANALYZE, BUFFERS, WAL, TIMING {timing}, SUMMARY ON, FORMAT JSON) ' + command.sql).fetchone()[0]
                    resources = parse_resources(raw)
                    checked = command.kind == 'select' or parse_one(command.sql, read='postgres').args.get('returning') is not None
                    if checked:
                        counts_match &= resources.output_rows == expected.row_count
                        if resources.output_rows > 10000:
                            raise ValueError('Профиль превысил 10000 выходных строк.')
                    readings.append(ResourceCommand(command.number, command.kind, resources, checked))
                control.check()
            _timeout(c, deadline, settings)
            c.execute(program.ending.upper())
        except BaseException:
            c.execute('ROLLBACK')
            raise
        final = state_digest(c, tables, settings, deadline, control)
        final_sequences = sequence_digest(c, tables, settings, deadline, control)
    metrics = [r.resources for r in readings if r.resources is not None]
    totals = {key: sum(r.counters[key] for r in metrics) if all(r.counters[key] is not None for r in metrics) else None for key in COUNTERS}
    states_match = ((initial, initial_sequences) == (normal.initial_state, normal.initial_sequences)
        and (final, final_sequences) == (normal.final_state, normal.final_sequences)
        and observed == normal.transaction_options and program.ending == normal.ending)
    return ProfileSample(database, block_size, work_mem_kb, tuple(readings), totals,
        max((r.max_operator_memory_kb for r in metrics if r.max_operator_memory_kb is not None), default=None),
        max((r.max_sort_disk_kb for r in metrics if r.max_sort_disk_kb is not None), default=None),
        initial, final, initial_sequences, final_sequences, observed, program.ending, size, build_ms, states_match, counts_match)


def _normal_signature(sample):
    return (tuple((c.number, c.kind, c.row_count, c.result_columns, c.result_sha256) for c in sample.commands),
        sample.initial_state, sample.initial_sequences, sample.final_state, sample.final_sequences,
        sample.transaction_options, sample.ending)


def run_transaction_resources(text, settings, reference_database, *, actions=(), number=None, variant_sql=None,
        work_mem_kb=None, test_copy_confirmed=False, disposable_databases_confirmed=False, control=None, recovery_directory=None):
    control = control or AnalysisControl()
    control.check()
    validate_reference(settings, reference_database, test_copy_confirmed, disposable_databases_confirmed)
    programs = prepare_resource_request(text, settings, actions, number, variant_sql, work_mem_kb)
    digest = resource_request_digest(text, settings, reference_database, actions, number, variant_sql, work_mem_kb)
    scratch, deadline = ScratchDatabases(settings, recovery_directory), time.monotonic() + 600
    variants, reasons, effective_memory, equivalent, status = [], [], None, None, 'error'
    try:
        with psycopg.connect(**_kwargs(settings, reference_database), autocommit=True) as c, control.monitor(c):
            with c.transaction(force_rollback=True):
                c.execute('SET TRANSACTION READ ONLY')
                _timeout(c, deadline, settings)
                if c.execute('SELECT pg_catalog.pg_database_size(current_database())').fetchone()[0] > MAX_DATABASE_BYTES:
                    raise ValueError('Эталон больше 256 МиБ.')
                for _, program, _ in programs:
                    replay_catalog(c, settings, program)
                defaults = read_transaction_options(c, defaults=True)
                effective_memory = work_mem_kb if work_mem_kb is not None else int(c.execute("SELECT pg_catalog.pg_size_bytes(current_setting('work_mem'))").fetchone()[0]) // 1024
                if not 64 <= effective_memory <= 65536:
                    raise ValueError('work_mem эталона выходит за пределы 64–65536 КиБ; задайте значение диагностики явно.')
        programs = tuple((kind, replace(p, options=p.options.resolve(defaults)), indexes) for kind, p, indexes in programs)
        seed = scratch.create(reference_database, enabled=False, deadline=deadline, control=control)
        for kind, program, indexes in programs:
            control.report(f'{kind}: обычный контрольный прогон на новой копии…')
            database = scratch.create(seed, enabled=True, deadline=deadline, control=control)
            normal = _sample(settings, database, program, indexes, 1, 'B' if indexes else 'A1', deadline, control,
                work_mem_kb=effective_memory, measure_backend=True)
            variants.append(ResourceVariant(kind, normal, None))
            if not scratch.remove(database):
                raise RuntimeError('Удаление контрольной копии не подтверждено.')
        if len(variants) == 2:
            equivalent = _normal_signature(variants[0].control) == _normal_signature(variants[1].control)
            if not equivalent:
                status = 'mismatch'
                reasons.append('Обычные прогоны исходного и изменённого SQL дали разные результаты или данные; профили ресурсов не сравнивались.')
        if equivalent is not False:
            for i, (kind, program, indexes) in enumerate(programs):
                control.report(f'{kind}: инструментированный прогон на другой свежей копии…')
                database = scratch.create(seed, enabled=True, deadline=deadline, control=control)
                profile = _profile_sample(settings, database, program, indexes, variants[i].control, effective_memory, deadline, control)
                variants[i] = replace(variants[i], profile=profile)
                if not scratch.remove(database):
                    raise RuntimeError('Удаление профилированной копии не подтверждено.')
            status = 'completed'
    except AnalysisCancelled:
        status = 'cancelled'
        reasons.append('Профиль отменён; неполные показатели не считаются результатом измерения.')
    except Exception as exc:
        status = 'declined' if isinstance(exc, ValueError) and not scratch.created else 'error'
        auth = password_failure(exc) if isinstance(exc, psycopg.OperationalError) else None
        reasons.append('Пароль не введён. Введите его в настройках подключения.' if auth == 'missing'
            else 'PostgreSQL отклонил пароль; проверьте настройки.' if auth == 'invalid'
            else str(exc) if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) else
            f'{type(exc).__name__}, SQLSTATE={getattr(exc, "sqlstate", None)}; профиль остановлен.')
    finally:
        cleanup, leftovers = scratch.finish()
    matched = bool(variants) and all(v.profile and v.profile.states_match_control and v.profile.output_counts_match_control for v in variants)
    if status == 'completed' and (not matched or not cleanup):
        status = 'mismatch' if cleanup else 'error'
        reasons.append('Данные/число строк профиля либо удаление копий не совпали с контролем; показатели не подтверждены.')
    reasons.extend(('Счётчики взяты из корней планов, без повторного сложения дочерних узлов. COMMIT, построение индексов и контрольные снимки в эти суммы не входят.',
        'Hit/read — операции с буферами, не число уникальных страниц и не гарантированное чтение физического диска.',
        'EXPLAIN не возвращает значения SELECT/RETURNING: в профиле сверены число выходных строк и итоговые таблицы/последовательности; значения проверены только между обычными прогонами.',
        'Память — максимум одного отчитавшегося оператора, не пиковая ОЗУ транзакции. Temp — объём блочного I/O, не размер уникальных файлов.',
        'Время EXPLAIN включает инструментирование. Один диагностический профиль не формирует рекомендацию по скорости и не измеряет конкуренцию сессий.'))
    return TransactionResources(status, reference_database, digest, effective_memory, tuple(variants), equivalent,
        bool(matched), cleanup, tuple(scratch.created), leftovers, tuple(reasons), str(scratch.manifest) if scratch.manifest else None)
