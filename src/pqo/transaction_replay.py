"""Real sequential COMMIT replay, exclusively on fresh tracked disposable databases.

Measures correctness/cleanup and assesses a fixed index set for this workload.
No connection ever targets source_settings.dbname. No deployment is performed.
"""
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from datetime import date, datetime
import argparse
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg import sql

from .analysis_control import AnalysisCancelled, AnalysisControl
from .backend_resources import BackendResourceMonitor, BackendResources
from .config import DatabaseSettings
from .index_actions import IndexAction, IndexActionKind
from .transaction_replay_guard import NAME, prepare_replay, replay_catalog
from .transaction_mixed_recommendation import MixedReplayPolicy, MixedRecommendation, assess_mixed_replay, replay_request_digest
from .transaction_context import CONTEXT_KINDS, CONTROL_KINDS, TransactionOptions, read_transaction_options


PREFIX = 'pqo_replay_'
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_STATE_BYTES = 256 * 1024 * 1024
MAX_STATE_ROWS = 2_000_000


@dataclass(frozen=True)
class StateDigest:
    table: str
    rows: int
    sha256: str
    sequences: tuple[str, ...] = ()


@dataclass(frozen=True)
class SequenceStateDigest:
    sequence: str
    sha256: str


@dataclass(frozen=True)
class CommandReplay:
    number: int
    kind: str
    client_ms: float
    row_count: int
    result_columns: tuple[tuple[str, int], ...]
    result_sha256: str | None


@dataclass(frozen=True)
class ReplaySample:
    series: int
    phase: str
    database: str
    commands: tuple[CommandReplay, ...]
    transaction_client_ms: float
    initial_state: tuple[StateDigest, ...]
    final_state: tuple[StateDigest, ...]
    index_size_bytes: int
    index_build_ms: float
    index_final_size_bytes: int = 0
    transaction_options: TransactionOptions | None = None
    ending: str = 'commit'
    initial_sequences: tuple[SequenceStateDigest, ...] = ()
    final_sequences: tuple[SequenceStateDigest, ...] = ()
    backend_resources: BackendResources | None = None


@dataclass(frozen=True)
class TransactionReplay:
    status: str  # completed, declined, error, cancelled
    reference_database: str
    actions: tuple[IndexAction, ...]
    samples: tuple[ReplaySample, ...]
    requested_series: int
    initial_states_equal: bool
    results_equal: bool
    final_states_equal: bool
    correctness_verified: bool
    cleanup_verified: bool
    created_databases: tuple[str, ...]
    leftover_databases: tuple[str, ...]
    reasons: tuple[str, ...]
    recovery_manifest: str | None = None
    request_digest: str = ''
    policy: MixedReplayPolicy | None = None
    recommendation: MixedRecommendation | None = None


def _kwargs(settings, database):
    return {**replace(settings, dbname=database, connect_timeout=min(5, max(1, settings.connect_timeout))).connection_kwargs(),
        'application_name': 'pqo_disposable_replay',
        'options': '-c search_path=pg_catalog -c row_security=on -c statement_timeout=30000 -c lock_timeout=1000'}


def _timeout(connection, deadline, settings):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError('Общий бюджет выполнения на одноразовых копиях исчерпан.')
    connection.execute(sql.SQL('SET statement_timeout = {}').format(
        sql.Literal(min(30_000, max(1, settings.statement_timeout_ms), remaining))))


class ScratchDatabases:
    """Only drop names successfully created here, with the SAME recorded OID/owner.

    No FORCE, wildcard cleanup, template overwrite, connection termination or
    source DB access. Ambiguous CREATE outcomes are retained, never guessed away.
    """
    def __init__(self, settings, recovery_directory=None):
        self.settings = settings
        self.created = {}
        self.removed = set()
        self.manifest = None
        if recovery_directory is not None:
            directory = Path(recovery_directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.manifest = directory / ('replay-' + uuid4().hex + '.json')
            self._record()

    def _record(self):
        if self.manifest is None:
            return
        data = {'version': 1, 'host': self.settings.host, 'port': self.settings.port,
            'user': self.settings.user, 'created': self.created, 'removed': sorted(self.removed),
            'note': 'Ownership evidence only; no SQL, data, passwords, or automatic recovery/drop.'}
        temporary = self.manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(self.manifest)

    def create(self, template, *, enabled, deadline, control):
        control.check()
        name = PREFIX + uuid4().hex
        with psycopg.connect(**_kwargs(self.settings, 'postgres'), autocommit=True) as connection:
            if connection.execute('SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s', (name,)).fetchone():
                raise FileExistsError('Случайное имя копии уже занято; существующая БД не изменена.')
            _timeout(connection, deadline, self.settings)
            self.created[name] = None
            self._record()  # persist intent before DDL, including ambiguous outcomes
            try:
                # Deliberately not interruptible mid-CREATE: cancellation is
                # checked after recording ownership, at most 30 s per CREATE.
                connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE {} ALLOW_CONNECTIONS false').format(
                    sql.Identifier(name), sql.Identifier(template)))
            except psycopg.errors.DuplicateDatabase:
                self.created.pop(name, None)
                raise
            identity = connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (name,)).fetchone()
            if not identity:
                raise RuntimeError('Не удалось установить OID созданной копии.')
            self.created[name] = tuple(identity)
            self._record()
            connection.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            if enabled:
                connection.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(name)))
        control.check()
        return name

    def remove(self, name):
        if name not in self.created or not name.startswith(PREFIX):
            raise ValueError('Удаление БД, не созданной текущей пробой, запрещено.')
        if name in self.removed:
            return True
        try:
            with psycopg.connect(**_kwargs(self.settings, 'postgres'), autocommit=True) as connection:
                # DROP can wait for a checkpoint even after our last session
                # has closed. Give cleanup the same bounded DDL budget as
                # CREATE; a short query/measurement timeout must not strand
                # our own database. Identity checks and no-FORCE still apply.
                connection.execute("SET statement_timeout='30s'")
                identity = connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (name,)).fetchone()
                if identity is None:
                    self.removed.add(name)
                    try:
                        self._record()
                    except OSError:
                        pass  # stale metadata never overrides independent absence
                    return True
                if self.created[name] is None or tuple(identity) != self.created[name]:
                    return False
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            # Independent connection confirms disappearance, not a successful
            # ROLLBACK or a local bookkeeping flag.
            with psycopg.connect(**_kwargs(self.settings, 'postgres'), autocommit=True) as check:
                absent = check.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s)', (name,)).fetchone()[0]
            if absent:
                self.removed.add(name)
                try:
                    self._record()
                except OSError:
                    pass
            return bool(absent)
        except psycopg.Error:
            return False

    def finish(self):
        if not self.created:
            return True, ()
        for name in reversed(self.created):
            self.remove(name)
        try:
            with psycopg.connect(**_kwargs(self.settings, 'postgres'), autocommit=True) as check:
                remaining = tuple(row[0] for row in check.execute(
                    'SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s) ORDER BY datname', (list(self.created),)).fetchall())
            return not remaining, remaining
        except psycopg.Error:
            return False, tuple(name for name in self.created if name not in self.removed)


def state_digest(connection, tables, settings, deadline, control):
    """Stream canonical PostgreSQL JSON rows in total PK order; never retain data."""
    states, rows_seen, bytes_seen = [], 0, 0
    with connection.transaction(force_rollback=True):
        connection.execute('SET TRANSACTION READ ONLY')
        for table in tables:
            control.check()
            _timeout(connection, deadline, settings)
            digest = hashlib.sha256()
            sequence_metadata = tuple((s.schema,s.name,s.column,s.dependency,s.configuration) for s in table.sequences)
            digest.update(json.dumps((table.columns,table.primary_key,table.default_signatures,sequence_metadata)).encode())
            count = 0
            statement = sql.SQL('SELECT pg_catalog.row_to_json(t)::text FROM {}.{} t ORDER BY {}').format(
                sql.Identifier(table.schema), sql.Identifier(table.name),
                sql.SQL(',').join(sql.Identifier(key) for key in table.primary_key))
            with connection.cursor(name='pqo_state_stream') as cursor:
                cursor.itersize = 2048
                cursor.execute(statement)
                for row in cursor:
                    control.check()
                    data = row[0].encode('utf-8')
                    count += 1
                    rows_seen += 1
                    bytes_seen += len(data)
                    if rows_seen > MAX_STATE_ROWS or bytes_seen > MAX_STATE_BYTES:
                        raise ValueError('Снимок состояния превысил лимит 2 млн строк / 256 МиБ.')
                    digest.update(len(data).to_bytes(8, 'big'))
                    digest.update(data)
                    if count % 2048 == 0:
                        _timeout(connection, deadline, settings)
            states.append(StateDigest(f'{table.schema}.{table.name}',count,digest.hexdigest(),
                tuple(f'{s.schema}.{s.name}' for s in table.sequences)))
    return tuple(states)


def sequence_digest(connection,tables,settings,deadline,control):
    """Read, never advance/reset, owned CACHE 1 sequences; hashes omit raw IDs.

    Sequences are non-MVCC state. Capture outside the user transaction, including
    after ROLLBACK: only row restoration is expected, not sequence restoration.
    """
    states=[]
    for table in tables:
        for sequence in table.sequences:
            control.check()
            _timeout(connection,deadline,settings)
            value=connection.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(
                sql.Identifier(sequence.schema),sql.Identifier(sequence.name))).fetchone()
            metadata=(sequence.schema,sequence.name,sequence.column,sequence.dependency,sequence.configuration,tuple(value))
            states.append(SequenceStateDigest(f'{sequence.schema}.{sequence.name}',
                hashlib.sha256(json.dumps(metadata).encode()).hexdigest()))
    return tuple(sorted(states,key=lambda state:state.sequence))


def _value(value):
    if value is None or type(value) in (str, int, bool):
        return (type(value).__name__, value)
    if isinstance(value, Decimal):
        return ('decimal', str(value))
    if isinstance(value, (datetime, date)):
        return (type(value).__name__, value.isoformat())
    raise ValueError('Неподдерживаемый тип результата; эквивалентность не установлена.')


def result_digest(rows, ordered):
    encoded = [json.dumps([_value(value) for value in row], ensure_ascii=False, separators=(',', ':')).encode('utf-8') for row in rows]
    if sum(map(len, encoded)) > 8 * 1024 * 1024:
        raise ValueError('Результат команды превысил 8 МиБ.')
    if not ordered:
        encoded.sort()  # duplicates retained, not a set
    digest = hashlib.sha256()
    for row in encoded:
        digest.update(len(row).to_bytes(8, 'big'))
        digest.update(row)
    return digest.hexdigest()


def _execute_program(connection, program, settings, deadline, control, *, report_progress=True):
    readings = []
    observed = None
    started = time.perf_counter()
    connection.execute(program.options.begin_sql)
    try:
        for command in program.commands:
            if report_progress:
                control.report(f'Последовательное выполнение: {command.number} {command.kind.upper()}; только одноразовая БД…')
            control.check()
            _timeout(connection, deadline, settings)
            if observed is None and command.kind not in CONTEXT_KINDS:
                observed = read_transaction_options(connection)
                expected = program.effective_options
                if any(value is not None and value != getattr(observed,key) for key,value in vars(expected).items()):
                    raise RuntimeError('Режимы транзакции PostgreSQL не соответствуют заявленным; прогон остановлен.')
            before = time.perf_counter()
            cursor = connection.execute(command.sql)
            columns = tuple((column.name, column.type_code) for column in cursor.description) if cursor.description else ()
            rows = cursor.fetchmany(10001) if columns else None
            elapsed = (time.perf_counter() - before) * 1000
            if cursor.rowcount > 10000 or rows is not None and len(rows) > 10000:
                raise ValueError('Команда превысила лимит 10000 возвращённых/затронутых строк.')
            count = 0 if command.kind in CONTROL_KINDS or command.kind == 'call' else cursor.rowcount
            readings.append(CommandReplay(command.number, command.kind, elapsed, count, columns,
                result_digest(rows, command.ordered) if rows is not None else None))
            control.check()
        _timeout(connection, deadline, settings)
        connection.execute(program.ending.upper())
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return tuple(readings), (time.perf_counter() - started) * 1000, observed


def _build_trial_indexes(connection, actions, settings, deadline, control):
    build_ms, size, created_indexes = 0., 0, []
    for action in actions:
        control.check()
        _timeout(connection, deadline, settings)
        index_name = PREFIX + 'idx_' + uuid4().hex
        ddl = sql.SQL('CREATE INDEX {} ON {}.{} ({})').format(sql.Identifier(index_name),
            sql.Identifier(action.schema_name), sql.Identifier(action.table_name),
            sql.SQL(',').join(sql.Identifier(key) for key in action.key_columns))
        if action.include_columns:
            ddl += sql.SQL(' INCLUDE ({})').format(sql.SQL(',').join(sql.Identifier(key) for key in action.include_columns))
        before = time.perf_counter()
        connection.execute(ddl)
        build_ms += (time.perf_counter() - before) * 1000
        created_indexes.append((action.schema_name, index_name))
        size += connection.execute('SELECT pg_catalog.pg_relation_size(c.oid) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s',
            (action.schema_name, index_name)).fetchone()[0]
        if size > 64 * 1024 * 1024:
            raise ValueError('Пробные индексы превысили лимит 64 МиБ после построения.')
    return build_ms, size, created_indexes


def _sample(settings, database, program, actions, series, phase, deadline, control, *, work_mem_kb=None, measure_backend=False):
    with psycopg.connect(**_kwargs(settings, database), autocommit=True) as connection, control.monitor(connection):
        for command in ("SET timezone='UTC'", "SET datestyle='ISO, YMD'", "SET standard_conforming_strings=on",
                        "SET jit=off", "SET max_parallel_workers_per_gather=0"):
            connection.execute(command)
        if work_mem_kb is not None:
            if type(work_mem_kb) is not int or not 64 <= work_mem_kb <= 65536:
                raise ValueError('work_mem для диагностики: от 64 до 65536 КиБ.')
            connection.execute(sql.SQL('SET work_mem={}').format(sql.Literal(f'{work_mem_kb}kB')))
        _timeout(connection, deadline, settings)
        tables = replay_catalog(connection, settings, program)
        metadata = {(table.schema, table.name): {name for name, _ in table.columns} for table in tables}
        for action in actions:
            if not set((*action.key_columns, *action.include_columns)) <= metadata.get((action.schema_name, action.table_name), set()):
                raise ValueError('Столбцы индекса отсутствуют в проверенной копии.')
        control.report(f'{phase}: снимок исходного состояния одноразовой БД…')
        initial = state_digest(connection, tables, settings, deadline, control)
        initial_sequences = sequence_digest(connection,tables,settings,deadline,control)
        build_ms, size, created_indexes = 0., 0, []
        if phase == 'B':
            build_ms, size, created_indexes = _build_trial_indexes(connection, actions, settings, deadline, control)
        backend_resources = None
        if measure_backend:
            with BackendResourceMonitor(connection) as monitor:
                commands, elapsed, observed = _execute_program(connection, program, settings, deadline, control)
            backend_resources = monitor.metrics
        else:
            commands, elapsed, observed = _execute_program(connection, program, settings, deadline, control)
        control.report(f'{phase}: проверка итоговых данных после {program.ending.upper()}…')
        final = state_digest(connection, tables, settings, deadline, control)
        final_sequences = sequence_digest(connection,tables,settings,deadline,control)
        final_size = 0
        for schema,name in created_indexes:
            control.check()
            _timeout(connection,deadline,settings)
            final_size += connection.execute('SELECT pg_catalog.pg_relation_size(c.oid) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s', (schema,name)).fetchone()[0]
    return ReplaySample(series,phase,database,commands,elapsed,initial,final,size,build_ms,final_size,observed,
        program.ending,initial_sequences,final_sequences,backend_resources)


def assess_correctness(samples, series):
    if len(samples) != 3 * series or [(item.series, item.phase) for item in samples] != [
            (number, phase) for number in range(1, series + 1) for phase in ('A1', 'B', 'A2')]:
        return False, False, False
    if len({(item.transaction_options,item.ending) for item in samples}) != 1:
        return False, False, False
    for sample in samples:
        for rows,states in ((sample.initial_state,sample.initial_sequences),(sample.final_state,sample.final_sequences)):
            expected=[name for table in rows for name in table.sequences]
            actual=[state.sequence for state in states]
            if len(set(expected))!=len(expected) or len(set(actual))!=len(actual) or set(expected)!=set(actual):
                return False,False,False
    initial = all((item.initial_state,item.initial_sequences) == (samples[0].initial_state,samples[0].initial_sequences) for item in samples)
    final = all((item.final_state,item.final_sequences) == (samples[0].final_state,samples[0].final_sequences) for item in samples)
    final = final and all(item.ending != 'rollback' or item.final_state == item.initial_state for item in samples)
    def outputs(sample):
        return tuple((item.number, item.kind, item.row_count, item.result_columns, item.result_sha256) for item in sample.commands)
    results = all(outputs(item) == outputs(samples[0]) for item in samples)
    return initial, results, final


def validate_reference(source_settings, reference_database, test_copy_confirmed, disposable_databases_confirmed):
    if (not test_copy_confirmed or not disposable_databases_confirmed or not isinstance(reference_database, str)
            or source_settings.dbname.casefold() in {'postgres', 'template0', 'template1'}
            or not NAME.fullmatch(reference_database) or reference_database.casefold() in {
                source_settings.dbname.casefold(), 'postgres', 'template0', 'template1'}
            or reference_database.casefold().startswith(PREFIX)):
        raise ValueError('Нужен отдельный лабораторный эталон и явное согласие на создание/удаление одноразовых копий; исходная БД запрещена.')


def run_transaction_replay(text, source_settings, reference_database, actions, *, test_copy_confirmed=False,
        disposable_databases_confirmed=False, series=1, control=None, recovery_directory=None, policy=None):
    """Create one frozen private seed, then 3 fresh clones per A1/B/A2 series.

    Only the clones execute supplied commands. No admin SQL is accepted from
    the input. Clone/index/verification time is excluded from client tx time;
    result transfer/hash and per-command timeout/control overhead are included.
    """
    control = control or AnalysisControl()
    control.check()
    validate_reference(source_settings,reference_database,test_copy_confirmed,disposable_databases_confirmed)
    if type(series) is not int or not 1 <= series <= 3:
        raise ValueError('Допускается 1–3 серии A1/B/A2.')
    program = prepare_replay(text, source_settings)
    policy = policy if policy is not None else MixedReplayPolicy()
    if not isinstance(policy,MixedReplayPolicy):
        raise ValueError('Нужна политика MixedReplayPolicy, зафиксированная до выполнения.')
    actions = tuple(actions)
    if (not 1 <= len(actions) <= 2 or len(set(actions)) != len(actions)
            or any(not isinstance(action, IndexAction) or action.kind is not IndexActionKind.CREATE
                or (action.schema_name, action.table_name) not in program.tables for action in actions)):
        raise ValueError('Нужны 1–2 разных CREATE-кандидата таблиц текущей транзакции.')
    request_digest = replay_request_digest(text,source_settings,reference_database,actions,series,policy)
    report = _run_disposable_programs(program,source_settings,reference_database,actions,series,control,recovery_directory)
    report = replace(report,request_digest=request_digest,policy=policy)
    return replace(report,recommendation=assess_mixed_replay(report,policy,expected_request_digest=request_digest))


def _run_disposable_programs(program, source_settings, reference_database, actions, series, control,
        recovery_directory, *, candidate_program=None):
    """Shared lifecycle for already guarded programs; no user SQL/admin shortcuts.

    Index trials use the same program in every phase. Rewrite trials pass a
    guarded replacement program in B only.
    Public entry points must validate/confirm before calling this private helper.
    """
    scratch, samples, reasons = ScratchDatabases(source_settings, recovery_directory), [], []
    status, deadline = 'error', time.monotonic() + 300
    try:
        control.report('Проверка каталогов лабораторного эталона; исходная БД не подключается…')
        with psycopg.connect(**_kwargs(source_settings, reference_database), autocommit=True) as reference, control.monitor(reference):
            with reference.transaction(force_rollback=True):
                reference.execute('SET TRANSACTION READ ONLY')
                _timeout(reference, deadline, source_settings)
                if reference.execute('SELECT pg_catalog.pg_database_size(current_database())').fetchone()[0] > MAX_DATABASE_BYTES:
                    raise ValueError('Лабораторная БД больше 256 МиБ; первый режим отказывается от клонирования.')
                replay_catalog(reference, source_settings, program)
                if candidate_program is not None:
                    replay_catalog(reference,source_settings,candidate_program)
                defaults = read_transaction_options(reference,defaults=True)
        program = replace(program,options=program.options.resolve(defaults))
        if candidate_program is not None:
            candidate_program = replace(candidate_program,options=candidate_program.options.resolve(defaults))
        control.report('Создание закрытого эталонного снимка; CREATE может занимать до 30 с…')
        seed = scratch.create(reference_database, enabled=False, deadline=deadline, control=control)
        for number in range(1, series + 1):
            for phase in ('A1', 'B', 'A2'):
                control.report(f'Серия {number}, {phase}: новая одноразовая копия того же снимка…')
                database = scratch.create(seed, enabled=True, deadline=deadline, control=control)
                selected = candidate_program if phase == 'B' and candidate_program is not None else program
                sample = _sample(source_settings, database, selected, actions, number, phase, deadline, control)
                samples.append(sample)
                if not scratch.remove(database):
                    raise RuntimeError('Удаление текущей одноразовой копии не подтверждено; дальнейшие прогоны остановлены.')
        status = 'completed'
    except AnalysisCancelled:
        status = 'cancelled'
        reasons.append('Операция отменена; статус удаления копий приведён отдельно, успешный откат не предполагается.')
    except Exception as exc:
        status = 'declined' if isinstance(exc, ValueError) else 'error'
        # No statement text/literals or psycopg detail (which can contain row data).
        if getattr(exc,'sqlstate',None) in {'40001','40P01'}:
            reasons.append(f'Конфликт транзакций, SQLSTATE={exc.sqlstate}; прогон остановлен без автоматического повтора. Эффект оптимизации не подтверждён.')
        else:
            reasons.append(str(exc) if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) else
                f'{type(exc).__name__}, SQLSTATE={getattr(exc, "sqlstate", None)}; выполнение остановлено.')
    finally:
        cleanup, leftovers = scratch.finish()
    initial, results, final = assess_correctness(samples, series)
    correct = status == 'completed' and initial and results and final and cleanup
    if status == 'completed' and not correct:
        reasons.append('Исходное состояние, результаты команд или итоговые данные различаются; корректность не подтверждена.')
    if not cleanup:
        if status == 'completed':
            status = 'error'
        reasons.append('Удаление всех одноразовых БД не подтверждено; проверьте перечисленные имена, автоматического FORCE нет.')
    reasons.append('Вывод относится только к измеренной нагрузке на копиях, не к production. Исходная БД не запускалась; постоянного применения нет.')
    return TransactionReplay(status, reference_database, actions, tuple(samples), series, initial, results, final,
        correct, cleanup, tuple(scratch.created), leftovers, tuple(reasons), str(scratch.manifest) if scratch.manifest else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sql_file', type=Path)
    parser.add_argument('--reference', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--table', required=True)
    parser.add_argument('--keys', required=True)
    parser.add_argument('--include', default='')
    parser.add_argument('--series', type=int, default=2)
    parser.add_argument('--minimum-gain-ms', type=float, default=5.)
    parser.add_argument('--minimum-gain-percent', type=float, default=5.)
    parser.add_argument('--maximum-index-mib', type=int, default=64)
    parser.add_argument('--recovery-directory', type=Path, default=Path('artifacts/disposable-recovery'))
    parser.add_argument('--confirm-laboratory-copy-and-disposable-databases', action='store_true')
    args = parser.parse_args()
    settings = replace(DatabaseSettings.from_env(), allowed_schemas=frozenset({args.schema}))
    action = IndexAction.create(args.schema, args.table, tuple(args.keys.split(',')), tuple(filter(None, args.include.split(','))))
    policy = MixedReplayPolicy(minimum_gain_ms=args.minimum_gain_ms,minimum_gain_ratio=args.minimum_gain_percent/100,
        maximum_index_bytes=args.maximum_index_mib*1024**2)
    report = run_transaction_replay(args.sql_file.read_text(encoding='utf-8-sig'), settings, args.reference, (action,),
        series=args.series, test_copy_confirmed=args.confirm_laboratory_copy_and_disposable_databases,
        disposable_databases_confirmed=args.confirm_laboratory_copy_and_disposable_databases,
        recovery_directory=args.recovery_directory,policy=policy)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.correctness_verified and report.cleanup_verified else 3


if __name__ == '__main__':
    raise SystemExit(main())
