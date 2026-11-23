"""Bounded multi-client transaction measurements on disposable laboratory copies."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from math import ceil
from statistics import median
from threading import Barrier, BrokenBarrierError
import time

import psycopg

from .analysis_control import AnalysisCancelled, AnalysisControl
from .connections import password_failure
from .transaction_context import ISOLATION_LEVELS, parse_transaction_control, read_transaction_options
from .transaction_replay import (
    MAX_DATABASE_BYTES, ScratchDatabases, _execute_program, _kwargs, _timeout,
    sequence_digest, state_digest, validate_reference,
)
from .transaction_replay_guard import prepare_replay, replay_catalog


RECOVERABLE = {'40001', '40P01', '55P03', '57014'}


@dataclass(frozen=True)
class LoadOptions:
    clients: int = 4
    repetitions: int = 4
    statement_ms: int = 5000
    compare_isolation: str | None = None

    def __post_init__(self):
        if type(self.clients) is not int or not 2 <= self.clients <= 12:
            raise ValueError('Нужно от 2 до 12 параллельных клиентов.')
        if (type(self.repetitions) is not int or not 1 <= self.repetitions <= 20
                or self.clients * self.repetitions > 120):
            raise ValueError('Допускается 1–20 транзакций на клиента и до 120 попыток на вариант.')
        if type(self.statement_ms) is not int or not 100 <= self.statement_ms <= 10_000:
            raise ValueError('Тайм-аут команды должен быть от 100 до 10000 мс.')
        if self.compare_isolation is not None and self.compare_isolation not in ISOLATION_LEVELS:
            raise ValueError('Неизвестный уровень изоляции для сравнения.')


@dataclass(frozen=True)
class LoadAttempt:
    client: int
    number: int
    outcome: str
    latency_ms: float
    sqlstate: str | None = None


@dataclass(frozen=True)
class LoadPhase:
    isolation: str
    elapsed_ms: float
    committed: int
    rolled_back: int
    failed: int
    completed_per_second: float
    p50_ms: float | None
    p95_ms: float | None
    attempts: tuple[LoadAttempt, ...]
    initial_state: tuple
    final_state: tuple
    initial_sequences: tuple
    final_sequences: tuple


@dataclass(frozen=True)
class TransactionLoad:
    status: str
    reference_database: str
    options: LoadOptions
    phases: tuple[LoadPhase, ...]
    cleanup_verified: bool
    created_databases: tuple[str, ...]
    leftover_databases: tuple[str, ...]
    reasons: tuple[str, ...]
    recovery_manifest: str | None


def _with_isolation(program, level):
    commands = []
    for command in program.commands:
        if command.kind == 'set_transaction':
            options = parse_transaction_control(command.sql).options
            if options.isolation_level is not None:
                options = replace(options, isolation_level=level)
                command = replace(command, sql='SET TRANSACTION ' + ', '.join(options.clauses))
        commands.append(command)
    return replace(program, options=replace(program.options, isolation_level=level), commands=tuple(commands))


def _run_clients(settings, database, program, options, deadline, control):
    started = []
    gate = Barrier(options.clients, action=lambda: started.append(time.perf_counter()), timeout=15)

    def client(number):
        attempts = []
        try:
            with psycopg.connect(**_kwargs(settings, database), autocommit=True) as c, control.monitor(c):
                for statement in ("SET timezone='UTC'", "SET datestyle='ISO, YMD'",
                                  'SET standard_conforming_strings=on', 'SET jit=off',
                                  'SET max_parallel_workers_per_gather=0'):
                    c.execute(statement)
                gate.wait()
                for repetition in range(1, options.repetitions + 1):
                    control.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Исчерпан общий бюджет прогона нагрузки.')
                    before = time.perf_counter()
                    try:
                        _, duration, _ = _execute_program(c, program, settings, deadline, control, report_progress=False)
                    except psycopg.Error as exc:
                        control.check()
                        attempts.append(LoadAttempt(number, repetition, 'failed',
                            (time.perf_counter() - before) * 1000, exc.sqlstate))
                        if exc.sqlstate not in RECOVERABLE:
                            break
                    else:
                        outcome = 'committed' if program.ending == 'commit' else 'rolled_back'
                        attempts.append(LoadAttempt(number, repetition, outcome, duration))
                return tuple(attempts), time.perf_counter()
        except BaseException:
            gate.abort()
            raise

    with ThreadPoolExecutor(max_workers=options.clients) as pool:
        futures = [pool.submit(client, number) for number in range(1, options.clients + 1)]
    control.check()
    errors = [f.exception() for f in futures if f.exception() is not None]
    if errors:
        raise next((exc for exc in errors if not isinstance(exc, BrokenBarrierError)), errors[0])
    groups, ended = zip(*(f.result() for f in futures), strict=True)
    return tuple(item for group in groups for item in group), (max(ended) - started[0]) * 1000


def _run_phase(settings, database, program, options, deadline, control):
    with psycopg.connect(**_kwargs(settings, database), autocommit=True) as c, control.monitor(c):
        _timeout(c, deadline, settings)
        tables = replay_catalog(c, settings, program)
        initial = state_digest(c, tables, settings, deadline, control)
        initial_sequences = sequence_digest(c, tables, settings, deadline, control)
    level = program.effective_options.isolation_level
    control.report(f'{level} · {options.clients} клиентов на общей копии…')
    attempts, elapsed = _run_clients(replace(settings, statement_timeout_ms=options.statement_ms), database,
                                    program, options, deadline, control)
    with psycopg.connect(**_kwargs(settings, database), autocommit=True) as c, control.monitor(c):
        final = state_digest(c, tables, settings, deadline, control)
        final_sequences = sequence_digest(c, tables, settings, deadline, control)
    completed = sorted(a.latency_ms for a in attempts if a.outcome != 'failed')
    committed = sum(a.outcome == 'committed' for a in attempts)
    rolled_back = len(completed) - committed
    return LoadPhase(level, elapsed, committed, rolled_back, len(attempts) - len(completed),
        len(completed) * 1000 / elapsed, median(completed) if completed else None,
        completed[ceil(0.95 * len(completed)) - 1] if completed else None, attempts,
        initial, final, initial_sequences, final_sequences)


def run_transaction_load(text, settings, reference_database, options=None, *,
        test_copy_confirmed=False, disposable_databases_confirmed=False, control=None, recovery_directory=None):
    control = control or AnalysisControl()
    control.check()
    validate_reference(settings, reference_database, test_copy_confirmed, disposable_databases_confirmed)
    options = options if options is not None else LoadOptions()
    if not isinstance(options, LoadOptions):
        raise ValueError('Задайте параметры прогона нагрузки.')
    program = prepare_replay(text, settings, allow_select_wrapper=True)
    scratch = ScratchDatabases(settings, recovery_directory)
    phases, reasons = [], []
    deadline = time.monotonic() + 120
    status = 'error'
    try:
        control.report('Проверка лабораторного эталона…')
        with psycopg.connect(**_kwargs(settings, reference_database), autocommit=True) as c, control.monitor(c):
            with c.transaction(force_rollback=True):
                c.execute('SET TRANSACTION READ ONLY')
                _timeout(c, deadline, settings)
                if c.execute('SELECT pg_catalog.pg_database_size(current_database())').fetchone()[0] > MAX_DATABASE_BYTES:
                    raise ValueError('Лабораторный эталон больше 256 МиБ.')
                replay_catalog(c, settings, program)
                defaults = read_transaction_options(c, defaults=True)
        program = replace(program, options=program.options.resolve(defaults))
        programs = [program]
        if options.compare_isolation:
            if options.compare_isolation == program.effective_options.isolation_level:
                raise ValueError('Выберите уровень изоляции, отличающийся от исходного.')
            programs.append(_with_isolation(program, options.compare_isolation))
        seed = scratch.create(reference_database, enabled=False, deadline=deadline, control=control)
        for item in programs:
            database = scratch.create(seed, enabled=True, deadline=deadline, control=control)
            phases.append(_run_phase(settings, database, item, options, deadline, control))
            if len(phases[-1].attempts) != options.clients * options.repetitions or any(
                    a.outcome == 'failed' and a.sqlstate not in RECOVERABLE for a in phases[-1].attempts):
                raise RuntimeError('Клиент остановился после ошибки SQL. Сравнение не завершено.')
        if len(phases) == 2 and (phases[0].initial_state, phases[0].initial_sequences) != (
                phases[1].initial_state, phases[1].initial_sequences):
            raise RuntimeError('Начальные состояния вариантов различаются. Сравнение недействительно.')
        status = 'completed'
    except AnalysisCancelled:
        status = 'cancelled'
        reasons.append('Прогон нагрузки отменён, клиентские сессии закрыты.')
    except Exception as exc:
        status = 'declined' if isinstance(exc, ValueError) and not scratch.created else 'error'
        auth = password_failure(exc) if isinstance(exc, psycopg.OperationalError) else None
        reasons.append('Проверьте пароль текущего подключения.' if auth else str(exc)
            if isinstance(exc, (RuntimeError, ValueError, TimeoutError)) else
            f'{type(exc).__name__}, SQLSTATE={getattr(exc, "sqlstate", None)}.')
    finally:
        cleanup, leftovers = scratch.finish()
    if not cleanup:
        status = 'error'
        reasons.append('Удаление всех одноразовых копий не подтверждено.')
    return TransactionLoad(status, reference_database, options, tuple(phases), cleanup, tuple(scratch.created),
                           leftovers, tuple(reasons), str(scratch.manifest) if scratch.manifest else None)
