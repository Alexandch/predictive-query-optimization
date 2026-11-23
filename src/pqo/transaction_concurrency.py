"""Two guarded sessions on one owned clone, with an explicit bounded schedule.

This is a concurrency diagnostic, not an equivalence proof, throughput benchmark
or authorization to lower isolation. Reports retain hashes, not SQL or row data.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import re
import time

import psycopg
from psycopg import sql

from .analysis_control import AnalysisCancelled, AnalysisControl
from .connections import password_failure
from .transaction_comparison import source_context_digest, sql_digest
from .transaction_context import CONTEXT_KINDS, CONTROL_KINDS, read_transaction_options
from .transaction_replay import (
    MAX_DATABASE_BYTES, ScratchDatabases, _kwargs, _timeout, result_digest,
    sequence_digest, state_digest, validate_reference,
)
from .transaction_replay_guard import prepare_replay, replay_catalog


CONFLICTS = frozenset({'40001', '40P01', '55P03', '57014'})


@dataclass(frozen=True)
class ScheduleStep:
    session: str
    command: int | str
    background: bool = False


@dataclass(frozen=True)
class ConcurrentCommand:
    number: int | str
    kind: str
    status: str
    client_ms: float
    row_count: int = 0
    result_columns: tuple = ()
    result_sha256: str | None = None
    sqlstate: str | None = None


@dataclass(frozen=True)
class ConcurrentSession:
    label: str
    outcome: str
    transaction_options: object
    client_ms: float
    commands: tuple[ConcurrentCommand, ...]


@dataclass(frozen=True)
class LockObservation:
    session: str
    command: int | str
    event: str
    blockers: tuple[str, ...]
    samples: int
    first_ms: float
    last_ms: float


@dataclass(frozen=True)
class ConcurrentReplay:
    status: str
    reference_database: str
    request_digest: str
    schedule: tuple[ScheduleStep, ...]
    sessions: tuple[ConcurrentSession, ...]
    locks: tuple[LockObservation, ...]
    initial_state: tuple
    final_state: tuple
    initial_sequences: tuple
    final_sequences: tuple
    cleanup_verified: bool
    created_databases: tuple[str, ...]
    leftover_databases: tuple[str, ...]
    reasons: tuple[str, ...]
    recovery_manifest: str | None


def prepare_schedule(text, programs):
    """A 1 / B END run to completion; '&' dispatches; WAIT joins or sees a lock."""
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError('Расписание: не более 4000 символов.')
    steps, next_number, pending, ended = [], {'A': 1, 'B': 1}, set(), set()
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip().upper()
        if not line:
            continue
        match = re.fullmatch(r'WAIT\s+([AB])(\s+LOCK)?|([AB])\s+(END|[1-9][0-9]?)(\s*&)?', line)
        if match is None:
            raise ValueError('Шаг расписания: A 1, B END, A 2 &, WAIT A или WAIT A LOCK.')
        if match[1]:
            label, command = match[1], 'LOCK' if match[2] else 'WAIT'
            if label not in pending:
                raise ValueError('WAIT требует незавершённого фонового шага той же сессии.')
            if command == 'WAIT':
                pending.remove(label)
            steps.append(ScheduleStep(label, command))
            continue
        label, value, background = match[3], match[4], bool(match[5])
        if label in pending or label in ended:
            raise ValueError('Дождитесь фонового шага; после END новые команды сессии запрещены.')
        size = len(programs[label].commands)
        command = value if value == 'END' else int(value)
        if command == 'END':
            if next_number[label] != size + 1:
                raise ValueError('END допускается только после всех команд сессии.')
            ended.add(label)
        elif command != next_number[label] or command > size:
            raise ValueError('Команды каждой сессии выполняются по номеру, ровно один раз, без пропусков.')
        else:
            next_number[label] += 1
        if background:
            pending.add(label)
        steps.append(ScheduleStep(label, command, background))
    if len(steps) > 100 or ended != {'A', 'B'} or pending:
        raise ValueError('Нужны END обеих сессий и WAIT всех фоновых шагов; не более 100 шагов.')
    return tuple(steps)


def prepare_concurrent(text_a, text_b, schedule, settings):
    if any(not isinstance(text, str) for text in (text_a, text_b)):
        raise ValueError('Нужны два текстовых SQL-блока.')
    try:
        programs = {'A': prepare_replay(text_a, settings), 'B': prepare_replay(text_b, settings)}
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        raise ValueError('Не удалось разобрать SQL сессий; ничего не выполнялось.') from None
    role_flags = [any(command.kind == 'set_role' for command in p.commands) for p in programs.values()]
    if role_flags[0] != role_flags[1]:
        raise ValueError('Если задан SET LOCAL ROLE, укажите явную роль в обеих сессиях.')
    steps = prepare_schedule(schedule, programs)
    return programs, steps


def concurrency_request_digest(text_a, text_b, schedule, settings, reference, statement_ms):
    programs, steps = prepare_concurrent(text_a, text_b, schedule, settings)
    if type(statement_ms) is not int or not 500 <= statement_ms <= 10000:
        raise ValueError('Лимит команды: от 500 до 10000 мс.')
    payload = ('concurrency-v1', sql_digest(text_a), sql_digest(text_b), [asdict(s) for s in steps],
        source_context_digest(settings), reference, statement_ms)
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class _Session:
    def __init__(self, label, connection, program, statement_ms):
        self.label, self.connection, self.program = label, connection, program
        self.commands, self.observed, self.outcome = [], None, 'open'
        self.current, self.statement_ms = 0, statement_ms
        self.elapsed = 0.
        # SET/SHOW do not acquire the first REPEATABLE READ snapshot.
        for command in ("SET timezone='UTC'", "SET datestyle='ISO, YMD'", "SET standard_conforming_strings=on",
                'SET jit=off', 'SET max_parallel_workers_per_gather=0', 'SET lock_timeout=0',
                'SET idle_in_transaction_session_timeout=60000'):
            connection.execute(command)
        self.started = time.perf_counter()
        connection.execute(program.options.begin_sql)

    def execute(self, number):
        self.current = number
        command = self.program.commands[number - 1] if type(number) is int else None
        kind = command.kind if command else self.program.ending
        if self.outcome != 'open':
            self.commands.append(ConcurrentCommand(number, kind, 'skipped', 0.))
            return
        before = time.perf_counter()
        try:
            self.connection.execute(sql.SQL('SET statement_timeout={}').format(sql.Literal(self.statement_ms)))
            if self.observed is None and (command is None or kind not in CONTEXT_KINDS):
                self.observed = read_transaction_options(self.connection)
                expected = self.program.effective_options
                if any(v is not None and v != getattr(self.observed, k) for k, v in vars(expected).items()):
                    raise RuntimeError('Фактическая изоляция/режим сессии не соответствует SQL.')
            cursor = self.connection.execute(command.sql if command else self.program.ending.upper())
            columns = tuple((c.name, c.type_code) for c in cursor.description) if cursor.description else ()
            rows = cursor.fetchmany(10001) if columns else None
            if cursor.rowcount > 10000 or rows is not None and len(rows) > 10000:
                raise ValueError('Команда превысила лимит 10000 возвращённых/затронутых строк.')
            self.commands.append(ConcurrentCommand(number, kind, 'completed', (time.perf_counter() - before) * 1000,
                0 if kind in CONTROL_KINDS or kind == 'call' or command is None else cursor.rowcount, columns,
                result_digest(rows, command.ordered) if rows is not None else None))
            if command is None:
                self.outcome = 'committed' if self.program.ending == 'commit' else 'rolled_back'
        except psycopg.Error as exc:
            self.commands.append(ConcurrentCommand(number, kind, 'conflict' if exc.sqlstate in CONFLICTS else 'error',
                (time.perf_counter() - before) * 1000, sqlstate=exc.sqlstate))
            self.outcome = 'aborted'
            self.connection.execute('ROLLBACK')
        except (ValueError, RuntimeError):
            self.commands.append(ConcurrentCommand(number, kind, 'error', (time.perf_counter() - before) * 1000))
            raise
        finally:
            if self.outcome != 'open':
                self.elapsed = (time.perf_counter() - self.started) * 1000

    def report(self):
        return ConcurrentSession(self.label, self.outcome, self.observed, self.elapsed, tuple(self.commands))


def _execute_schedule(settings, database, programs, steps, statement_ms, deadline, control, evidence):
    sessions, connections, futures, observations = {}, [], {}, {}
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pqo-session')
    started = time.perf_counter()
    try:
        for label, program in programs.items():
            connection = psycopg.connect(**_kwargs(settings, database), autocommit=True)
            connections.append(connection)
            sessions[label] = _Session(label, connection, program, statement_ms)
        observer = psycopg.connect(**_kwargs(settings, database), autocommit=True)
        connections.append(observer)
        observer.execute('SET statement_timeout=1000')
        pids = {s.connection.info.backend_pid: label for label, s in sessions.items()}

        def check():
            control.check()
            if time.monotonic() > deadline:
                raise TimeoutError('Бюджет параллельного сценария исчерпан.')

        def observe():
            check()
            rows = observer.execute('''SELECT pid,wait_event,pg_catalog.pg_blocking_pids(pid)
                FROM pg_catalog.pg_stat_activity WHERE pid=ANY(%s) AND wait_event_type='Lock' ''',
                (list(pids),)).fetchall()
            seen = set()
            for pid, event, blockers in rows:
                label = pids[pid]
                blocking = tuple(sorted(pids[b] for b in blockers if b in pids))
                if not blocking:
                    continue
                seen.add(label)
                now = (time.perf_counter() - started) * 1000
                key = (label, sessions[label].current, event, blocking)
                previous = observations.get(key)
                observations[key] = (replace(previous, samples=previous.samples + 1, last_ms=now) if previous
                    else LockObservation(label, sessions[label].current, event, blocking, 1, now, now))
            return seen

        def wait(label, lock=False):
            future = futures[label]
            until = min(deadline, time.monotonic() + statement_ms / 1000 + 2)
            while True:
                seen = observe()
                if lock and label in seen:
                    return
                if future.done():
                    future.result()
                    if lock:
                        raise ValueError('Ожидаемая блокировка не наблюдалась: фоновый шаг уже завершился. Измените расписание.')
                    futures.pop(label)
                    return
                if time.monotonic() > until:
                    raise TimeoutError('Фоновый шаг не завершился в заданный срок.')
                time.sleep(.02)

        for step in steps:
            check()
            control.report(f'Две сессии: {step.session} {step.command}' + (' в фоне…' if step.background else '…'))
            if step.command in {'WAIT', 'LOCK'}:
                wait(step.session, step.command == 'LOCK')
            else:
                futures[step.session] = executor.submit(sessions[step.session].execute, step.command)
                if not step.background:
                    wait(step.session)
        return tuple(s.report() for s in sessions.values()), tuple(observations.values())
    finally:
        # Cancel only our two connections. Never terminate backends by PID.
        for label, future in list(futures.items()):
            if not future.done():
                try:
                    sessions[label].connection.cancel_safe(timeout=2)
                except Exception:
                    pass  # Per-command statement_timeout also bounds the worker.
        executor.shutdown(wait=True, cancel_futures=True)
        for connection in reversed(connections):
            connection.close()  # Open/failed transactions are rolled back by disconnect.
        for session in sessions.values():
            if session.outcome == 'open':
                session.outcome = 'interrupted'
                session.elapsed = (time.perf_counter() - session.started) * 1000
        evidence.update(sessions=tuple(s.report() for s in sessions.values()), locks=tuple(observations.values()))


def run_concurrent_transactions(text_a, text_b, schedule, settings, reference_database, *,
        statement_ms=5000, test_copy_confirmed=False, disposable_databases_confirmed=False,
        control=None, recovery_directory=None):
    control = control or AnalysisControl()
    control.check()
    validate_reference(settings, reference_database, test_copy_confirmed, disposable_databases_confirmed)
    programs, steps = prepare_concurrent(text_a, text_b, schedule, settings)
    digest = concurrency_request_digest(text_a, text_b, schedule, settings, reference_database, statement_ms)
    combined = replace(programs['A'], commands=programs['A'].commands + programs['B'].commands,
        tables=tuple(sorted(set(programs['A'].tables) | set(programs['B'].tables))))
    scratch, deadline = ScratchDatabases(settings, recovery_directory), time.monotonic() + 120
    sessions, locks, initial, final, initial_sequences, final_sequences = (), (), (), (), (), ()
    status, reasons = 'error', []
    evidence = {}
    try:
        control.report('Проверка каталогов эталона для обеих сессий…')
        with psycopg.connect(**_kwargs(settings, reference_database), autocommit=True) as reference, control.monitor(reference):
            with reference.transaction(force_rollback=True):
                reference.execute('SET TRANSACTION READ ONLY')
                _timeout(reference, deadline, settings)
                if reference.execute('SELECT pg_catalog.pg_database_size(current_database())').fetchone()[0] > MAX_DATABASE_BYTES:
                    raise ValueError('Лабораторный эталон больше 256 МиБ.')
                replay_catalog(reference, settings, combined)
                defaults = read_transaction_options(reference, defaults=True)
        programs = {label: replace(p, options=p.options.resolve(defaults)) for label, p in programs.items()}
        seed = scratch.create(reference_database, enabled=False, deadline=deadline, control=control)
        database = scratch.create(seed, enabled=True, deadline=deadline, control=control)
        with psycopg.connect(**_kwargs(settings, database), autocommit=True) as connection, control.monitor(connection):
            tables = replay_catalog(connection, settings, combined)
            initial = state_digest(connection, tables, settings, deadline, control)
            initial_sequences = sequence_digest(connection, tables, settings, deadline, control)
        sessions, locks = _execute_schedule(settings, database, programs, steps, statement_ms, deadline, control, evidence)
        control.report('Обе сессии закрыты. Снимок итоговых таблиц и последовательностей…')
        with psycopg.connect(**_kwargs(settings, database), autocommit=True) as connection, control.monitor(connection):
            final = state_digest(connection, tables, settings, deadline, control)
            final_sequences = sequence_digest(connection, tables, settings, deadline, control)
        status = 'error' if any(c.status == 'error' for s in sessions for c in s.commands) else 'completed'
        states = sorted({c.sqlstate for s in sessions for c in s.commands if c.sqlstate})
        if states:
            reasons.append('Зафиксированы SQLSTATE: ' + ', '.join(states) + '. Автоматических повторов не было.')
        reasons.append('Блокировки между A и B наблюдались.' if locks else 'Блокировки между A и B не наблюдались; короткие ожидания могли не попасть в выборку.')
    except AnalysisCancelled:
        status = 'cancelled'
        reasons.append('Сценарий отменён; сессии закрыты. Результат выполнения не считается полным.')
    except Exception as exc:
        status = 'declined' if isinstance(exc, ValueError) and not evidence else 'error'
        auth = password_failure(exc) if isinstance(exc, psycopg.OperationalError) else None
        reasons.append('Пароль не введён. Введите его в настройках текущего подключения и повторите проверку.' if auth == 'missing'
            else 'PostgreSQL отклонил пароль. Проверьте пароль текущего подключения в настройках.' if auth == 'invalid'
            else str(exc) if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) else
            f'{type(exc).__name__}, SQLSTATE={getattr(exc, "sqlstate", None)}; сценарий остановлен.')
    finally:
        cleanup, leftovers = scratch.finish()
    sessions, locks = evidence.get('sessions', sessions), evidence.get('locks', locks)
    if not cleanup:
        status = 'error' if status == 'completed' else status
        reasons.append('Удаление всех собственных копий не подтверждено; проверьте перечисленные имена.')
    reasons.append('Время включает заданные расписанием паузы. Это один сценарий двух сессий, без вывода об оптимальном уровне изоляции или эквивалентности изменений.')
    return ConcurrentReplay(status, reference_database, digest, steps, sessions, locks, initial, final,
        initial_sequences, final_sequences, cleanup, tuple(scratch.created), leftovers, tuple(reasons),
        str(scratch.manifest) if scratch.manifest else None)
