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
MINIMUM_ACTIVE_SAMPLES = 3
MINIMUM_FACTOR = 0.05
MAXIMUM_FACTOR = 20.0


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    sql_hash: str
    predicted_time_ms: float
    actual_time_ms: float
    measured_at: str


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
        if not self.observations:
            return 1.0
        ratios_by_query: dict[str, list[float]] = {}
        for item in self.observations:
            ratios_by_query.setdefault(item.sql_hash, []).append(
                math.log1p(item.actual_time_ms)
                - math.log1p(item.predicted_time_ms)
            )
        query_medians = [median(values) for values in ratios_by_query.values()]
        return min(
            MAXIMUM_FACTOR,
            max(MINIMUM_FACTOR, math.exp(median(query_medians))),
        )

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
            abs(item.actual_time_ms - apply_calibration(item.predicted_time_ms, self))
            for item in self.observations
        ) / self.sample_count


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    profile: CalibrationProfile
    observation: CalibrationObservation


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


def apply_calibration(predicted_time_ms: float, profile: CalibrationProfile) -> float:
    if not profile.ready:
        return float(predicted_time_ms)
    return max(0.0, (float(predicted_time_ms) + 1.0) * profile.factor - 1.0)


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
