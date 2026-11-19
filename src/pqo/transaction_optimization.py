"""Four transaction variants measured from one frozen, owned seed database."""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from statistics import median
import time

import psycopg

from .analysis_control import AnalysisCancelled, AnalysisControl
from .index_actions import IndexAction, IndexActionKind
from .transaction_comparison import source_context_digest, sql_digest
from .transaction_context import read_transaction_options
from .transaction_mixed_recommendation import MixedReplayPolicy, assess_mixed_replay
from .transaction_replay import (
    MAX_DATABASE_BYTES,
    ReplaySample,
    ScratchDatabases,
    TransactionReplay,
    _kwargs,
    _sample,
    _timeout,
    assess_correctness,
    validate_reference,
)
from .transaction_replay_guard import replay_catalog
from .transaction_select_rewrite import prepare_select_replacement

VARIANTS = ("indexes", "rewrite", "combined")
TITLES = {
    "original": "Исходная транзакция",
    "indexes": "Только индексы",
    "rewrite": "Только переписывание",
    "combined": "Индексы и переписывание",
}
ORDERS = (VARIANTS, tuple(reversed(VARIANTS)), ("rewrite", "indexes", "combined"))


@dataclass(frozen=True)
class OptimizationObservation:
    variant: str
    sample: ReplaySample


@dataclass(frozen=True)
class OptimizationVariant:
    kind: str
    measurement: TransactionReplay


@dataclass(frozen=True)
class TransactionOptimization:
    status: str
    reference_database: str
    select_number: int
    requested_series: int
    request_digest: str
    policy: MixedReplayPolicy
    observations: tuple[OptimizationObservation, ...]
    variants: tuple[OptimizationVariant, ...]
    comparable: bool
    cleanup_verified: bool
    created_databases: tuple[str, ...]
    leftover_databases: tuple[str, ...]
    recovery_manifest: str | None
    decision: str
    best_variant: str | None
    eligible_variants: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def original_time_ms(self):
        values = [
            x.sample.transaction_client_ms
            for x in self.observations
            if x.variant == "original"
        ]
        return median(values) if values else None


def optimization_request_digest(
    text, settings, reference, number, variant_sql, actions, series, policy
):
    # Guard and normalize before recording provenance; never retain raw SQL or passwords.
    prepared = prepare_select_replacement(text, settings, number, variant_sql)
    payload = (
        "transaction-optimization-v1",
        sql_digest(text),
        source_context_digest(settings),
        reference,
        number,
        sql_digest(prepared.variant_sql),
        [asdict(x) for x in actions],
        series,
        asdict(policy),
    )
    return sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def select_best_variant(variants, comparable, policy):
    if not comparable:
        return (
            "inconclusive",
            None,
            (),
            ("Общая исходная точка, полные прогоны или очистка не подтверждены.",),
        )
    reports = {x.kind: x.measurement for x in variants}
    eligible = tuple(
        k
        for k in VARIANTS
        if reports[k].recommendation.recommended
        and (
            k != "combined"
            or all(reports[x].correctness_verified for x in ("indexes", "rewrite"))
        )
    )
    if not eligible:
        uncertain = any(
            x.recommendation.status == "inconclusive" for x in reports.values()
        )
        return (
            ("inconclusive" if uncertain else "not_recommended"),
            None,
            (),
            (
                "Ни один вариант не прошёл все условия корректности, выигрыша и стабильности.",
            ),
        )
    best = min(
        eligible, key=lambda key: reports[key].recommendation.summary[0].indexed_ms
    )
    # Ranking a few noisy medians must not manufacture a unique winner.
    for other in eligible:
        if other == best:
            continue
        for a, b in zip(
            reports[best].recommendation.series, reports[other].recommendation.series
        ):
            fast, slow = a.metrics[0].indexed_ms, b.metrics[0].indexed_ms
            if (
                slow <= fast
                or slow - fast < policy.minimum_gain_ms
                or (slow - fast) / max(slow, 0.001) < policy.minimum_gain_ratio
            ):
                return (
                    "multiple",
                    None,
                    eligible,
                    (
                        "Есть полезные варианты, но устойчивый отрыв одного из них не подтверждён.",
                    ),
                )
    return (
        "recommended",
        best,
        eligible,
        (
            "Выбран самый быстрый подтверждённый вариант; при наличии альтернатив отрыв прошёл оба порога в каждой серии.",
        ),
    )


def run_transaction_optimization(
    text,
    settings,
    reference,
    number,
    variant_sql,
    actions,
    *,
    series=2,
    test_copy_confirmed=False,
    disposable_databases_confirmed=False,
    policy=None,
    control=None,
    recovery_directory=None,
):
    control = control or AnalysisControl()
    control.check()
    validate_reference(
        settings, reference, test_copy_confirmed, disposable_databases_confirmed
    )
    if type(series) is not int or not 1 <= series <= 3:
        raise ValueError(
            "Допускается 1–3 серии; для итоговой рекомендации нужны минимум две."
        )
    policy = policy if policy is not None else MixedReplayPolicy()
    if not isinstance(policy, MixedReplayPolicy):
        raise ValueError("Нужна пороговая политика, зафиксированная до запуска.")
    prepared = prepare_select_replacement(text, settings, number, variant_sql)
    actions = tuple(actions)
    if (
        not 1 <= len(actions) <= 2
        or any(
            not isinstance(x, IndexAction)
            or x.kind is not IndexActionKind.CREATE
            or (x.schema_name, x.table_name) not in prepared.original.tables
            for x in actions
        )
        or len(set(actions)) != len(actions)
    ):
        raise ValueError(
            "Отметьте 1–2 разных CREATE-кандидата таблиц текущей транзакции."
        )
    digest = optimization_request_digest(
        text, settings, reference, number, variant_sql, actions, series, policy
    )
    scratch = ScratchDatabases(settings, recovery_directory)
    observations, reasons = [], []
    pairs = {k: [] for k in VARIANTS}
    seed, status = None, "error"
    deadline = time.monotonic() + 600
    original, variant = prepared.original, prepared.variant
    try:
        control.report("Проверка эталона и обоих вариантов SQL…")
        with (
            psycopg.connect(
                **_kwargs(settings, reference), autocommit=True
            ) as connection,
            control.monitor(connection),
        ):
            with connection.transaction(force_rollback=True):
                connection.execute("SET TRANSACTION READ ONLY")
                _timeout(connection, deadline, settings)
                if (
                    connection.execute(
                        "SELECT pg_catalog.pg_database_size(current_database())"
                    ).fetchone()[0]
                    > MAX_DATABASE_BYTES
                ):
                    raise ValueError(
                        "Лабораторная БД больше 256 МиБ; сравнение не запускается."
                    )
                replay_catalog(connection, settings, original)
                replay_catalog(connection, settings, variant)
                defaults = read_transaction_options(connection, defaults=True)
        original = replace(original, options=original.options.resolve(defaults))
        variant = replace(variant, options=variant.options.resolve(defaults))
        control.report("Создаю единый закрытый снимок для четырёх вариантов…")
        seed = scratch.create(
            reference, enabled=False, deadline=deadline, control=control
        )

        def measure(kind, iteration):
            control.report(
                f"Серия {iteration}: {TITLES[kind]}; новая копия общего снимка…"
            )
            database = scratch.create(
                seed, enabled=True, deadline=deadline, control=control
            )
            program = variant if kind in {"rewrite", "combined"} else original
            indexes = actions if kind in {"indexes", "combined"} else ()
            sample = _sample(
                settings,
                database,
                program,
                indexes,
                iteration,
                "A1" if kind == "original" else "B",
                deadline,
                control,
            )
            observations.append(OptimizationObservation(kind, sample))
            if not scratch.remove(database):
                raise RuntimeError(
                    "Удаление текущей копии не подтверждено; сравнение остановлено."
                )
            return sample

        for iteration in range(1, series + 1):
            before = measure("original", iteration)
            for kind in ORDERS[iteration - 1]:
                middle = measure(kind, iteration)
                after = measure("original", iteration)
                pairs[kind].extend(
                    (replace(before, phase="A1"), middle, replace(after, phase="A2"))
                )
                before = after
        control.check()
        status = "completed"
    except AnalysisCancelled:
        status = "cancelled"
        reasons.append("Сравнение отменено; положительный итог не формируется.")
    except Exception as exc:
        status = "declined" if isinstance(exc, ValueError) else "error"
        reasons.append(
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError, TimeoutError))
            else f"{type(exc).__name__}, SQLSTATE={getattr(exc, 'sqlstate', None)}; сравнение остановлено."
        )
    finally:
        cleanup, leftovers = scratch.finish()
    if not cleanup:
        status = "error"
        reasons.append("Удаление всех одноразовых БД не подтверждено.")
    # Compare all baseline states/results, not only each adjacent triple.
    baselines = tuple(
        replace(x.sample, series=1, phase="A1")
        for x in observations
        if x.variant == "original"
    )
    initial = {
        (x.sample.initial_state, x.sample.initial_sequences) for x in observations
    }
    baseline_outputs = {
        tuple(
            (c.number, c.kind, c.row_count, c.result_columns, c.result_sha256)
            for c in s.commands
        )
        for s in baselines
    }
    baseline_final = {
        (s.final_state, s.final_sequences, s.transaction_options, s.ending)
        for s in baselines
    }
    expected_order = tuple(
        k
        for i in range(series)
        for k in ("original", *(x for kind in ORDERS[i] for x in (kind, "original")))
    )
    comparable = (
        status == "completed"
        and cleanup
        and len(initial) == len(baseline_outputs) == len(baseline_final) == 1
        and tuple(x.variant for x in observations) == expected_order
        and len(scratch.created) == 1 + 7 * series
        and len({x.sample.database for x in observations}) == 7 * series
    )
    reports = []
    for kind in VARIANTS:
        samples = tuple(pairs[kind])
        a, r, f = assess_correctness(samples, series)
        variant_digest = sha256((digest + ":" + kind).encode()).hexdigest()
        report = TransactionReplay(
            status,
            reference,
            actions if kind != "rewrite" else (),
            samples,
            series,
            a,
            r,
            f,
            comparable and a and r and f,
            cleanup,
            tuple(([seed] if seed else []) + [s.database for s in samples]),
            leftovers,
            tuple(reasons),
            str(scratch.manifest) if scratch.manifest else None,
            variant_digest,
            policy,
        )
        report = replace(
            report,
            recommendation=assess_mixed_replay(
                report, policy, expected_request_digest=variant_digest, mode=kind
            ),
        )
        reports.append(OptimizationVariant(kind, report))
    decision, best, eligible, assessment = select_best_variant(
        tuple(reports), comparable, policy
    )
    return TransactionOptimization(
        status,
        reference,
        number,
        series,
        digest,
        policy,
        tuple(observations),
        tuple(reports),
        comparable,
        cleanup,
        tuple(scratch.created),
        leftovers,
        str(scratch.manifest) if scratch.manifest else None,
        decision,
        best,
        eligible,
        tuple(reasons) + assessment,
    )
