"""Database-specific calibration for query-time predictions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import median

from .config import DatabaseSettings


PROFILE_VERSION = 1
MINIMUM_ACTIVE_SAMPLES = 10
MINIMUM_SEGMENT_SAMPLES = 3
SEGMENT_SHRINKAGE = 5.0
MINIMUM_FACTOR = 0.05
MAXIMUM_FACTOR = 20.0


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    sql_hash: str
    predicted_time_ms: float
    actual_time_ms: float
    measured_at: str
    segment: str = ""
    shape_hash: str = ""


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    profile_version: int
    database_identity: str
    model_sha256: str
    observations: tuple[CalibrationObservation, ...]

    @property
    def sample_count(self) -> int:
        return len(self.observations)

    @property
    def unique_query_count(self) -> int:
        return len({item.sql_hash for item in self.observations})

    @property
    def ready(self) -> bool:
        return self.unique_query_count >= MINIMUM_ACTIVE_SAMPLES

    @property
    def factor(self) -> float:
        return _weighted_factor(self.observations)

    @property
    def segment_sample_counts(self) -> dict[str, int]:
        return {
            segment: len(
                {
                    item.sql_hash
                    for item in self.observations
                    if item.segment == segment
                }
            )
            for segment in sorted(
                {item.segment for item in self.observations if item.segment}
            )
        }

    @property
    def active_segment_count(self) -> int:
        return sum(
            count >= MINIMUM_SEGMENT_SAMPLES
            for count in self.segment_sample_counts.values()
        )

    @property
    def seen_shape_count(self) -> int:
        return len({item.shape_hash for item in self.observations if item.shape_hash})

    @property
    def base_mae_ms(self) -> float | None:
        if not self.observations:
            return None
        return sum(
            abs(item.actual_time_ms - item.predicted_time_ms)
            for item in self.observations
        ) / self.sample_count

    @property
    def calibrated_mae_ms(self) -> float | None:
        if not self.observations:
            return None
        return sum(
            abs(
                item.actual_time_ms
                - _apply_factor(
                    item.predicted_time_ms,
                    factor_for_segment(self, item.segment),
                )
            )
            for item in self.observations
        ) / self.sample_count

    @property
    def improves_mae(self) -> bool:
        base = self.base_mae_ms
        calibrated = self.calibrated_mae_ms
        return bool(
            self.ready
            and base is not None
            and calibrated is not None
            and calibrated < base
        )

    @property
    def mae_improvement_percent(self) -> float | None:
        base = self.base_mae_ms
        calibrated = self.calibrated_mae_ms
        if base is None or calibrated is None or base <= 0:
            return None
        return (base - calibrated) / base * 100.0


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    profile: CalibrationProfile
    observation: CalibrationObservation


@dataclass(frozen=True, slots=True)
class CalibrationBatchResult:
    profile: CalibrationProfile
    added_query_count: int
    base_mae_ms: float | None
    calibrated_mae_ms: float | None
    mae_improvement_percent: float | None


def database_identity(settings: DatabaseSettings) -> str:
    """Return a password-free identity for compatibility checks."""
    return f"{settings.host.lower()}:{settings.port}/{settings.dbname}"


def model_sha256(model_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(model_path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def suggested_profile_path(
    directory: str | Path,
    settings: DatabaseSettings,
    model_path: str | Path,
) -> Path:
    identity_hash = hashlib.sha256(database_identity(settings).encode()).hexdigest()[:12]
    model_hash = model_sha256(model_path)[:12].lower()
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in settings.dbname
    ).strip("_") or "database"
    return Path(directory) / f"{safe_name}-{identity_hash}-{model_hash}.json"


def new_profile(
    settings: DatabaseSettings,
    model_path: str | Path,
) -> CalibrationProfile:
    return CalibrationProfile(
        profile_version=PROFILE_VERSION,
        database_identity=database_identity(settings),
        model_sha256=model_sha256(model_path),
        observations=(),
    )


def save_profile(profile: CalibrationProfile, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_profile(path: str | Path) -> CalibrationProfile:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("profile_version") != PROFILE_VERSION:
        raise ValueError("Unsupported calibration profile version")
    observations = tuple(
        CalibrationObservation(
            sql_hash=str(item["sql_hash"]),
            predicted_time_ms=float(item["predicted_time_ms"]),
            actual_time_ms=float(item["actual_time_ms"]),
            measured_at=str(item["measured_at"]),
            segment=str(item.get("segment", "")),
            shape_hash=str(item.get("shape_hash", "")),
        )
        for item in data.get("observations", ())
    )
    if any(
        item.predicted_time_ms < 0
        or item.actual_time_ms < 0
        or not math.isfinite(item.predicted_time_ms)
        or not math.isfinite(item.actual_time_ms)
        for item in observations
    ):
        raise ValueError("Calibration observations must contain finite non-negative times")
    return CalibrationProfile(
        profile_version=PROFILE_VERSION,
        database_identity=str(data["database_identity"]),
        model_sha256=str(data["model_sha256"]).upper(),
        observations=observations,
    )


def validate_profile(
    profile: CalibrationProfile,
    settings: DatabaseSettings,
    model_path: str | Path,
) -> None:
    if profile.database_identity != database_identity(settings):
        raise ValueError("Calibration profile belongs to another database")
    if profile.model_sha256 != model_sha256(model_path):
        raise ValueError("Calibration profile belongs to another XGBoost model")


def add_observation(
    profile: CalibrationProfile,
    sql_text: str,
    predicted_time_ms: float,
    actual_time_ms: float,
) -> tuple[CalibrationProfile, CalibrationObservation]:
    if min(predicted_time_ms, actual_time_ms) < 0:
        raise ValueError("Calibration times must be non-negative")
    if not all(math.isfinite(value) for value in (predicted_time_ms, actual_time_ms)):
        raise ValueError("Calibration times must be finite")
    observation = CalibrationObservation(
        sql_hash=hashlib.sha256(sql_text.strip().encode()).hexdigest(),
        predicted_time_ms=float(predicted_time_ms),
        actual_time_ms=float(actual_time_ms),
        measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        segment=query_segment(sql_text, predicted_time_ms),
        shape_hash=query_shape_hash(sql_text),
    )
    return (
        CalibrationProfile(
            profile_version=profile.profile_version,
            database_identity=profile.database_identity,
            model_sha256=profile.model_sha256,
            observations=profile.observations + (observation,),
        ),
        observation,
    )


def _weighted_factor(observations: tuple[CalibrationObservation, ...]) -> float:
    if not observations:
        return 1.0
    observations_by_query: dict[str, list[CalibrationObservation]] = {}
    for item in observations:
        observations_by_query.setdefault(item.sql_hash, []).append(item)
    ratios = []
    weights = []
    for repeated in observations_by_query.values():
        ratios.append(
            median(
                (item.actual_time_ms + 1.0) / (item.predicted_time_ms + 1.0)
                for item in repeated
            )
        )
        weights.append(median(item.predicted_time_ms + 1.0 for item in repeated))
    ordered = sorted(zip(ratios, weights, strict=True))
    midpoint = sum(weights) / 2.0
    cumulative = 0.0
    factor = ordered[-1][0]
    for ratio, weight in ordered:
        cumulative += weight
        if cumulative >= midpoint:
            factor = ratio
            break
    return min(MAXIMUM_FACTOR, max(MINIMUM_FACTOR, factor))


def query_segment(sql_text: str, predicted_time_ms: float) -> str:
    """Return a deliberately coarse structural and predicted-latency segment."""
    from .sql_features import extract_sql_features

    features = extract_sql_features(sql_text)
    join_band = min(features.join_count, 3)
    has_subquery = int(features.subquery_count > 0)
    has_aggregate = int(
        features.aggregate_function_count > 0 or features.has_group_by
    )
    has_order = int(features.has_order_by)
    if predicted_time_ms < 100.0:
        latency_band = "fast"
    elif predicted_time_ms < 1000.0:
        latency_band = "medium"
    else:
        latency_band = "heavy"
    return (
        f"j{join_band}-s{has_subquery}-a{has_aggregate}-o{has_order}"
        f"-{latency_band}"
    )


def query_shape_hash(sql_text: str) -> str:
    """Hash a PostgreSQL AST after replacing literal parameter values."""
    from sqlglot import exp, parse_one

    tree = parse_one(sql_text.strip(), read="postgres")

    def anonymize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal):
            return exp.Literal.string("__value__")
        return node

    normalized = tree.transform(anonymize).sql(dialect="postgres", pretty=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


def factor_for_segment(profile: CalibrationProfile, segment: str) -> float:
    """Return a shrinkage-adjusted factor, or identity for sparse segments."""
    if not profile.ready or not segment:
        return 1.0
    observations = tuple(
        item for item in profile.observations if item.segment == segment
    )
    unique_count = len({item.sql_hash for item in observations})
    if unique_count < MINIMUM_SEGMENT_SAMPLES:
        return 1.0
    raw_factor = _weighted_factor(observations)
    confidence = unique_count / (unique_count + SEGMENT_SHRINKAGE)
    return raw_factor**confidence


def factor_for_query(
    profile: CalibrationProfile,
    sql_text: str,
    predicted_time_ms: float,
) -> float:
    if not profile.improves_mae:
        return 1.0
    shape_hash = query_shape_hash(sql_text)
    if not any(item.shape_hash == shape_hash for item in profile.observations):
        return 1.0
    return factor_for_segment(profile, query_segment(sql_text, predicted_time_ms))


def _apply_factor(predicted_time_ms: float, factor: float) -> float:
    return max(0.0, (float(predicted_time_ms) + 1.0) * factor - 1.0)


def apply_calibration(
    predicted_time_ms: float,
    profile: CalibrationProfile,
    sql_text: str | None = None,
) -> float:
    if not profile.improves_mae:
        return float(predicted_time_ms)
    factor = (
        factor_for_query(profile, sql_text, predicted_time_ms)
        if sql_text is not None
        else profile.factor
    )
    return _apply_factor(predicted_time_ms, factor)


def calibrate_query(
    sql_text: str,
    model_path: str | Path,
    profile_path: str | Path,
    settings: DatabaseSettings | None = None,
) -> CalibrationResult:
    """Execute one read-only query and append its measured calibration pair."""
    from .explain import collect_explain
    from .prediction import predict_sql_query

    settings = settings or DatabaseSettings.from_env()
    profile_file = Path(profile_path)
    if profile_file.is_file():
        profile = load_profile(profile_file)
        validate_profile(profile, settings, model_path)
    else:
        profile = new_profile(settings, model_path)

    prediction = predict_sql_query(sql_text, model_path, settings=settings)
    measured = collect_explain(sql_text, settings=settings, analyze=True)
    actual_time = measured.features.actual_total_time_ms
    if actual_time is None:
        raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
    profile, observation = add_observation(
        profile,
        sql_text,
        prediction.predicted_time_ms,
        actual_time,
    )
    save_profile(profile, profile_file)
    return CalibrationResult(profile=profile, observation=observation)


def parse_calibration_workload(
    sql_text: str,
    *,
    maximum_queries: int = 30,
) -> tuple[str, ...]:
    """Parse and deduplicate a semicolon-separated read-only workload."""
    from sqlglot import parse

    from .explain import _assert_read_only_query

    if maximum_queries <= 0:
        raise ValueError("maximum_queries must be positive")
    queries = []
    seen = set()
    for expression in parse(sql_text, read="postgres"):
        if expression is None:
            continue
        normalized = _assert_read_only_query(
            expression.sql(dialect="postgres", pretty=False, comments=False)
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        queries.append(normalized)
        if len(queries) > maximum_queries:
            raise ValueError(
                f"Calibration workload contains more than {maximum_queries} queries"
            )
    if not queries:
        raise ValueError("Calibration workload contains no SQL queries")
    return tuple(queries)


def calibrate_workload(
    queries: tuple[str, ...],
    model_path: str | Path,
    profile_path: str | Path,
    settings: DatabaseSettings | None = None,
) -> CalibrationBatchResult:
    """Measure a bounded read-only workload and update one calibration profile."""
    from .explain import _assert_read_only_query, collect_explain
    from .prediction import predict_sql_query

    if not 1 <= len(queries) <= 30:
        raise ValueError("Calibration workload must contain from 1 to 30 queries")
    settings = settings or DatabaseSettings.from_env()
    profile_file = Path(profile_path)
    if profile_file.is_file():
        profile = load_profile(profile_file)
        validate_profile(profile, settings, model_path)
    else:
        profile = new_profile(settings, model_path)

    normalized_queries = tuple(_assert_read_only_query(item) for item in queries)
    if len(set(normalized_queries)) != len(normalized_queries):
        raise ValueError("Calibration workload contains duplicate SQL queries")

    for sql_text in normalized_queries:
        prediction = predict_sql_query(sql_text, model_path, settings=settings)
        measured = collect_explain(sql_text, settings=settings, analyze=True)
        actual_time = measured.features.actual_total_time_ms
        if actual_time is None:
            raise RuntimeError("EXPLAIN ANALYZE returned no execution time")
        profile, _ = add_observation(
            profile,
            sql_text,
            prediction.predicted_time_ms,
            actual_time,
        )
        save_profile(profile, profile_file)

    return CalibrationBatchResult(
        profile=profile,
        added_query_count=len(normalized_queries),
        base_mae_ms=profile.base_mae_ms,
        calibrated_mae_ms=profile.calibrated_mae_ms,
        mae_improvement_percent=profile.mae_improvement_percent,
    )
