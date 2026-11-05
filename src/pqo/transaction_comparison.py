"""Bounded comparison of index singles/pairs, SELECT only, on a confirmed copy."""
from dataclasses import dataclass
from itertools import combinations
from hashlib import sha256
import json
import math
import time

from .analysis_control import AnalysisCancelled, AnalysisControl
from .index_actions import IndexActionKind
from .transaction_index_trial import (
    _read_slice, run_transaction_index_set_trial, validate_frequencies,
)


@dataclass(frozen=True)
class TransactionIndexComparison:
    copy_database: str
    frequencies: tuple[tuple[int, int], ...]
    trials: tuple
    expected_variants: int  # expected trial rows across all requested series
    completed: bool
    preferred_variant: int | None  # zero-based first-series trial row, NOT a deployment authorization
    reasons: tuple[str, ...]
    pair_exclusions: tuple[tuple[int, str], ...] = ()
    requested_series: int = 1
    completed_series: int = 0
    # One entry per trial row: (1-based series, stable 1-based variant number).
    coordinates: tuple[tuple[int, int], ...] = ()
    series_preferences: tuple[int | None, ...] = ()  # stable 1-based variant numbers
    development_sql_digest: str = ""
    source_context_digest: str = ""

    def coordinate(self, row):
        return self.coordinates[row] if self.coordinates else (1, row + 1)

    @property
    def stable_for_reads(self):
        return self.requested_series > 1 and self.completed and self.preferred_variant is not None


def source_context_digest(settings):
    """Bind evidence to connection/guard settings, never serialize a password."""
    context = (settings.host, settings.port, settings.dbname, settings.user,
        sorted(settings.allowed_schemas), settings.connect_timeout, settings.statement_timeout_ms)
    return sha256(json.dumps(context, ensure_ascii=True).encode("utf-8")).hexdigest()


def sql_digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def index_variants(actions):
    actions = tuple(actions)
    if (not 1 <= len(actions) <= 3 or len(set(actions)) != len(actions)
            or any(action.kind is not IndexActionKind.CREATE for action in actions)):
        raise ValueError("Отметьте от одного до трёх разных индексных кандидатов; проверяются одиночные варианты и пары.")
    return tuple((action,) for action in actions) + tuple(combinations(actions, 2))


def _baseline_problem(trials):
    """Same unweighted drift guard within AND across series; never pool samples."""
    measured = [trial for trial in trials if trial.status == "measured" and trial.rollback_verified]
    if not measured:
        return "Нет полных измерений для сопоставления."
    numbers = {item.number for item in measured[0].readings}
    weights = measured[0].frequencies
    if any({item.number for item in trial.readings} != numbers or trial.frequencies != weights for trial in measured):
        return "Варианты имеют разные SELECT/числа повторений; сопоставление недопустимо."
    for number in sorted(numbers):
        values = [next(item.baseline_ms for item in trial.readings if item.number == number) for trial in measured]
        if max(values) - min(values) > 2 and (max(values) - min(values)) / max(min(values), .001) > .20:
            return f"SELECT {number}: исходное время различается между вариантами/сериями; предпочтение не выбрано."
    return None


def comparison_preference(trials, *, minimum_gain_ms, minimum_gain_ratio):
    """Conservative relative-gain comparison, not a statistically proven optimum."""
    problem = _baseline_problem(trials)
    if problem:
        return None, (problem,), ()
    eligible = [i for i, trial in enumerate(trials) if trial.status == "measured"
        and trial.accepted_for_reads and trial.rollback_verified]
    excluded = []
    for i in eligible:
        trial = trials[i]
        if len(trial.actions) == 1:
            continue
        singles = [other for other in trials if len(other.actions) == 1
            and other.actions[0] in trial.actions and other.accepted_for_reads and other.rollback_verified
            and other.status == "measured"]
        if singles:
            best = max(singles, key=lambda other: other.weighted_gain_ms)
            extra_gain = trial.weighted_gain_ms - best.weighted_gain_ms
            if extra_gain < minimum_gain_ms or extra_gain / max(best.weighted_indexed_ms, .001) < minimum_gain_ratio:
                excluded.append((i, "Сочетание не доказало добавочную пользу относительно лучшего одиночного индекса по обоим порогам."))
    eligible = [i for i in eligible if i not in {number for number, _ in excluded}]
    if not eligible:
        return None, ("Нет подтверждённого варианта для чтений с обоснованным составом индексов.",), tuple(excluded)
    eligible.sort(key=lambda i: (-trials[i].weighted_gain_ms, len(trials[i].actions), trials[i].index_size_bytes))
    first = trials[eligible[0]]
    if len(eligible) > 1:
        second = trials[eligible[1]]
        margin = first.weighted_gain_ms - second.weighted_gain_ms
        if margin < minimum_gain_ms or margin / max(second.weighted_indexed_ms, .001) < minimum_gain_ratio:
            return None, ("У подтверждённых вариантов близкие выигрыши; однозначное предпочтение не выбрано.",), tuple(excluded)
    return eligible[0], (), tuple(excluded)


def compare_transaction_indexes(sql_text, source_settings, copy_database, actions, *, frequencies=None,
        test_copy_confirmed=False, repetitions=3, minimum_gain_ms=1., minimum_gain_ratio=.05,
        series_count=1, control=None):
    """1..3 series, at most 18 A/B/A trials, ONE shared 180s budget plus cleanup.

    Frequencies weight medians only, they do not replay SELECTs that many times.
    Each variant is rolled back before the next; DML is never sent for execution.
    Series use forward, reverse, then rotated order, with stable variant numbers.
    Every series must independently prefer the same variant; cross-series drift
    also blocks preference. Repeated evidence is NOT an independent holdout set.
    No source connection, model updates, permanent indexes or transaction-time claim.
    """
    control = control or AnalysisControl()
    control.check()
    actions = tuple(actions)
    variants = index_variants(actions)
    copy_database = copy_database.strip()
    if not test_copy_confirmed or not copy_database or copy_database.casefold() == source_settings.dbname.casefold():
        raise ValueError("Требуются другая БД и явное подтверждение отдельной тестовой копии.")
    if (type(series_count) is not int or not 1 <= series_count <= 3
            or type(repetitions) is not int or not 1 <= repetitions <= 5
            or not math.isfinite(minimum_gain_ms) or minimum_gain_ms < 0
            or not math.isfinite(minimum_gain_ratio) or not 0 <= minimum_gain_ratio <= 1
            or any(action.schema_name not in source_settings.allowed_schemas for variant in variants for action in variant)):
        raise ValueError("Некорректные параметры сравнения/схема.")
    # Preflight every action and the complete SELECT slice before any connection.
    for action in tuple(actions):
        queries, _ = _read_slice(sql_text, source_settings, action)
    weights = validate_frequencies((number for number, _ in queries), frequencies)
    deadline = time.monotonic() + 180
    trials, reasons, coordinates, preferences, exclusions = [], [], [], [], []
    stopped = False
    for series in range(series_count):
        order = list(range(len(variants)))
        if series == 1:
            order.reverse()
        elif series == 2:
            order = order[1:] + order[:1]
        start = len(trials)
        for number in order:
            if control.cancelled.is_set():
                reasons.append("Сравнение отменено; предпочтительный вариант не выбирается.")
                stopped = True
                break
            if time.monotonic() >= deadline:
                reasons.append("Общий бюджет сравнения 180 с исчерпан; все серии не завершены.")
                stopped = True
                break
            variant = variants[number]
            try:
                control.report(f"Серия {series + 1}/{series_count}, вариант {number + 1}/{len(variants)}: индексов {len(variant)}…")
                trial = run_transaction_index_set_trial(sql_text, source_settings, copy_database, variant,
                    test_copy_confirmed=True, repetitions=repetitions, minimum_gain_ms=minimum_gain_ms,
                    minimum_gain_ratio=minimum_gain_ratio, frequencies=weights, deadline=deadline, control=control)
            except AnalysisCancelled:
                reasons.append("Сравнение отменено до начала следующей пробы.")
                stopped = True
                break
            trials.append(trial)
            coordinates.append((series + 1, number + 1))
            if trial.status in {"error", "cancelled"} or not trial.rollback_verified:
                reasons.append("Серия остановлена после ошибки/отмены или неподтверждённого отката; предпочтения нет.")
                stopped = True
                break
        if control.cancelled.is_set() and not stopped:
            reasons.append("Сравнение отменено; предпочтительный вариант не выбирается.")
            stopped = True
        if stopped:
            break
        local, notes, pair_notes = comparison_preference(trials[start:],
            minimum_gain_ms=minimum_gain_ms, minimum_gain_ratio=minimum_gain_ratio)
        preferences.append(None if local is None else coordinates[start + local][1])
        reasons.extend(f"Серия {series + 1}: {note}" for note in notes)
        exclusions.extend((start + row, note) for row, note in pair_notes)
    completed = len(preferences) == series_count and not stopped
    preferred = None
    if completed:
        problem = _baseline_problem(trials)
        if problem:
            reasons.append("Проверка между сериями: " + problem)
        elif preferences[0] is not None and all(value == preferences[0] for value in preferences):
            preferred = next(row for row, coordinate in enumerate(coordinates) if coordinate == (1, preferences[0]))
        elif series_count > 1:
            reasons.append("Устойчивое предпочтение не подтверждено: серии не выбрали один и тот же вариант.")
    return TransactionIndexComparison(copy_database, weights, tuple(trials), len(variants) * series_count,
        completed, preferred, tuple(reasons), tuple(exclusions), series_count, len(preferences),
        tuple(coordinates), tuple(preferences), sql_digest(sql_text), source_context_digest(source_settings))
