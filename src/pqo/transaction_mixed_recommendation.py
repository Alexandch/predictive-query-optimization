"""Conservative, fixed-policy assessment of measured mixed COMMIT workloads.

No database access, ranking, index application or statistical/production claim.
The selected set and policy are fixed BEFORE replay. One series is diagnostic.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from statistics import median

from .transaction_comparison import source_context_digest, sql_digest
from .index_actions import IndexAction, IndexActionKind


@dataclass(frozen=True)
class MixedReplayPolicy:
    minimum_gain_ms: float = 5.
    minimum_gain_ratio: float = .05
    regression_ms: float = 2.
    regression_ratio: float = .10
    drift_ms: float = 2.
    drift_ratio: float = .20
    maximum_index_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for name in ('minimum_gain_ms', 'regression_ms', 'drift_ms'):
            if not _finite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError('Порог времени должен быть конечным неотрицательным числом.')
        for name in ('minimum_gain_ratio', 'regression_ratio', 'drift_ratio'):
            if not _finite(getattr(self, name)) or not 0 <= getattr(self, name) <= 1:
                raise ValueError('Относительный порог должен находиться в диапазоне 0–1.')
        if type(self.maximum_index_bytes) is not int or not 1 <= self.maximum_index_bytes <= 64 * 1024 * 1024:
            raise ValueError('Бюджет новых индексов: от 1 байта до 64 МиБ.')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def replay_request_digest(text, settings, reference, actions, series, policy):
    """Evidence binding, NOT authorization/authentication. Never include password/SQL."""
    payload = ('mixed-replay-v1', sql_digest(text), source_context_digest(settings), reference,
        [asdict(action) for action in actions], series, asdict(policy))
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class MixedMetric:
    label: str
    baseline_ms: float
    indexed_ms: float

    @property
    def gain_ms(self):
        return self.baseline_ms - self.indexed_ms

    @property
    def gain_ratio(self):
        return self.gain_ms / max(self.baseline_ms, .001)


@dataclass(frozen=True)
class MixedSeries:
    number: int
    metrics: tuple[MixedMetric, ...]  # transaction, SELECT sum, DML sum, each command
    gain_passed: bool
    regression_reasons: tuple[str, ...]
    drift_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MixedRecommendation:
    status: str  # recommended / not_recommended / inconclusive
    policy: MixedReplayPolicy
    request_digest: str
    series: tuple[MixedSeries, ...]
    summary: tuple[MixedMetric, ...]  # medians of series metrics, not pooled runs
    maximum_index_bytes: int | None
    median_build_ms: float | None
    reasons: tuple[str, ...]

    @property
    def recommended(self):
        return self.status == 'recommended'


def _values(sample):
    commands = sample.commands
    return (sample.transaction_client_ms,
        sum(item.client_ms for item in commands if item.kind == 'select'),
        sum(item.client_ms for item in commands if item.kind != 'select'),
        *(item.client_ms for item in commands))


def _exceeds(low, high, absolute, ratio):
    return high - low > absolute and (high - low) / max(low, .001) > ratio


def _evidence_problem(report, mode):
    from .transaction_replay import assess_correctness
    if type(report.requested_series) is not int or not 1 <= report.requested_series <= 3:
        return 'Некорректное число серий.'
    if (report.status != 'completed' or not report.correctness_verified or not report.cleanup_verified
            or not all((report.initial_states_equal,report.results_equal,report.final_states_equal))
            or report.leftover_databases or not all(assess_correctness(report.samples, report.requested_series))):
        return 'Полное выполнение, равенство результатов/данных и удаление копий не подтверждены.'
    samples = report.samples
    if (not samples or type(report.requested_series) is not int or not 1 <= report.requested_series <= 3
            or mode == 'indexes' and not 1 <= len(report.actions) <= 2
            or mode == 'rewrite' and bool(report.actions)
            or len(set(report.actions)) != len(report.actions)
            or any(not isinstance(action,IndexAction) or action.kind is not IndexActionKind.CREATE for action in report.actions)
            or len(report.created_databases) != 1 + len(samples)
            or len(set(report.created_databases)) != len(report.created_databases)
            or len({item.database for item in samples}) != len(samples)
            or any(item.database not in report.created_databases or item.database == report.reference_database for item in samples)):
        return 'Неполные координаты/набор индексов или нет отдельных копий для всех фаз.'
    commands = samples[0].commands
    if (not commands or [item.number for item in commands] != list(range(1, len(commands) + 1))
            or not any(item.kind == 'select' for item in commands)
            or not any(item.kind in {'insert','update','delete'} for item in commands)):
        return 'Нет полного смешанного набора SELECT и DML.'
    for sample in samples:
        if (not _finite(sample.transaction_client_ms) or sample.transaction_client_ms <= 0
                or not _finite(sample.index_build_ms) or sample.index_build_ms < 0
                or type(sample.index_size_bytes) is not int or sample.index_size_bytes < 0
                or type(sample.index_final_size_bytes) is not int or sample.index_final_size_bytes < 0
                or mode == 'indexes' and sample.phase == 'B' and (sample.index_size_bytes <= 0 or sample.index_final_size_bytes <= 0)
                or mode == 'rewrite' and any((sample.index_size_bytes,sample.index_final_size_bytes,sample.index_build_ms))
                or sample.phase != 'B' and any((sample.index_size_bytes, sample.index_final_size_bytes, sample.index_build_ms))):
            return 'Невалидные времена/размеры индексов; пропуски не заменяются нулями.'
        if any(item.kind not in {'select','insert','update','delete'} or not _finite(item.client_ms)
                or item.client_ms < 0 or type(item.row_count) is not int or item.row_count < 0 for item in sample.commands):
            return 'Невалидное измерение отдельной команды.'
        if sum(item.client_ms for item in sample.commands) > sample.transaction_client_ms + .000001:
            return 'Сумма execute+fetch превышает общий клиентский интервал.'
        for states in (sample.initial_state, sample.final_state):
            if (not states or len({state.table for state in states}) != len(states)
                    or any(type(state.rows) is not int or state.rows < 0 or not _digest(state.sha256) for state in states)):
                return 'Нет полных контрольных снимков таблиц.'
        if {state.table for state in sample.initial_state} != {state.table for state in sample.final_state}:
            return 'Состав таблиц начального и конечного снимков различается.'
        if any((item.kind == 'select' or item.result_columns) and not _digest(item.result_sha256) for item in sample.commands):
            return 'Нет контрольных результатов SELECT/RETURNING.'
    return None


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def assess_mixed_replay(report, policy, *, expected_request_digest, mode='indexes'):
    """Do not pool away a failed series, DML regression or baseline drift.

    Totals are client BEGIN..COMMIT; command sums are execute+fetch only.
    Thresholds are engineering guards, not confidence intervals. No frequency
    extrapolation, mixed holdout, cold-cache or concurrency evidence is implied.
    """
    empty = lambda reason: MixedRecommendation('inconclusive', policy, report.request_digest, (), (), None, None, (reason,))
    if not isinstance(policy, MixedReplayPolicy):
        raise ValueError('Нужна политика, зафиксированная до выполнения.')
    if mode not in {'indexes','rewrite'}:
        raise ValueError('Неизвестный режим измеренной нагрузки.')
    if not _digest(report.request_digest) or report.request_digest != expected_request_digest or report.policy != policy:
        return empty('Замеры не связаны с текущими SQL/подключением/эталоном/индексами/политикой. Нужен новый прогон.')
    problem = _evidence_problem(report,mode)
    if problem:
        return empty(problem)
    samples = report.samples
    labels = ('Транзакция BEGIN–COMMIT', 'SELECT суммарно', 'DML суммарно',
        *(f'Команда {item.number} {item.kind.upper()}' for item in samples[0].commands))
    series, baselines, indexed = [], [], []
    for offset in range(0, len(samples), 3):
        a1, b, a2 = samples[offset:offset+3]
        first, last, middle = _values(a1), _values(a2), _values(b)
        baseline = tuple((x+y)/2 for x,y in zip(first,last))
        baselines.append(baseline)
        indexed.append(middle)
        metrics = tuple(MixedMetric(label,base,after) for label,base,after in zip(labels,baseline,middle))
        drift = tuple(f'Серия {a1.series}, {label}: дрейф A1/A2.' for label,x,y in zip(labels,first,last)
            if _exceeds(min(x,y),max(x,y),policy.drift_ms,policy.drift_ratio))
        regression = tuple(f'Серия {a1.series}, {metric.label}: замедление {(-metric.gain_ms):.3f} мс / {(-metric.gain_ratio)*100:.1f}%.'
            for metric in metrics[1:] if _exceeds(metric.baseline_ms,metric.indexed_ms,policy.regression_ms,policy.regression_ratio))
        gain = metrics[0].gain_ms > 0 and metrics[0].gain_ms >= policy.minimum_gain_ms and metrics[0].gain_ratio >= policy.minimum_gain_ratio
        series.append(MixedSeries(a1.series, metrics, gain, regression, drift))
    summary = tuple(MixedMetric(label,median(row[index] for row in baselines),median(row[index] for row in indexed)) for index,label in enumerate(labels))
    reasons = [reason for item in series for reason in item.drift_reasons]
    for name, values in (('исходных',baselines),('индексных',indexed)):
        for index,label in enumerate(labels):
            low,high = min(row[index] for row in values),max(row[index] for row in values)
            if _exceeds(low,high,policy.drift_ms,policy.drift_ratio):
                reasons.append(f'{label}: нестабильность {name} времён между сериями.')
    if report.requested_series < 2:
        reasons.append('Одна серия — только диагностика; для рекомендации нужны минимум две полные серии.')
    inconclusive = bool(reasons)
    reasons.extend(reason for item in series for reason in item.regression_reasons)
    reasons.extend(f'Серия {item.number}: общий выигрыш не прошёл оба заданных порога.' for item in series if not item.gain_passed)
    size = max(max(item.index_size_bytes,item.index_final_size_bytes) for item in samples)
    build = median(item.index_build_ms for item in samples if item.phase == 'B')
    if size > policy.maximum_index_bytes:
        reasons.append('Новые индексы превышают зафиксированный бюджет размера (до/после DML).')
    status = 'inconclusive' if inconclusive else 'not_recommended' if reasons else 'recommended'
    if not reasons:
        reasons.append('В каждой серии общий выигрыш прошёл оба порога; значимых регрессий команд и дрейфа нет.')
    return MixedRecommendation(status,policy,report.request_digest,tuple(series),summary,size,build,tuple(reasons))
