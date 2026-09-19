"""UI-independent query-time prediction service."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .config import DatabaseSettings
from .database_features import collect_database_features
from .explain import collect_explain
from .sql_features import extract_sql_features
from .training import predict_query_time


@dataclass(frozen=True, slots=True)
class QueryTimePrediction:
    sql_text: str
    predicted_time_ms: float
    estimated_total_cost: float
    estimated_plan_rows: float
    root_node_type: str
    plan_node_count: int
    uncalibrated_time_ms: float
    calibration_factor: float
    calibration_sample_count: int
    error_mae_ms: float | None = None
    error_source: str | None = None

    @property
    def uncertainty_lower_ms(self) -> float | None:
        if self.error_mae_ms is None:
            return None
        return max(0.0, self.predicted_time_ms - self.error_mae_ms)

    @property
    def uncertainty_upper_ms(self) -> float | None:
        if self.error_mae_ms is None:
            return None
        return self.predicted_time_ms + self.error_mae_ms


def model_error_mae_ms(model_path: str | Path) -> float | None:
    """Read the parameter-holdout MAE stored next to a trained model."""
    metrics_path = Path(model_path).resolve().parent / "metrics.json"
    if not metrics_path.is_file():
        return None
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        value = float(metrics["parameter_holdout"]["mae_ms"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value >= 0 else None


def predict_sql_query(
    sql_text: str,
    model_path: str | Path,
    settings: DatabaseSettings | None = None,
    calibration_profile_path: str | Path | None = None,
) -> QueryTimePrediction:
    """Predict without executing the user's query."""
    settings = settings or DatabaseSettings.from_env()
    sql_features = extract_sql_features(sql_text)
    estimated_plan = collect_explain(sql_text, settings=settings, analyze=False)
    database_features = collect_database_features(
        sql_text,
        estimated_plan.features.estimated_plan_rows,
        settings=settings,
    )
    feature_values = {
        **sql_features.as_dict(),
        **estimated_plan.features.as_dict(),
        **database_features.as_dict(),
    }
    uncalibrated_time_ms = predict_query_time(model_path, feature_values)
    predicted_time_ms = uncalibrated_time_ms
    calibration_factor = 1.0
    calibration_sample_count = 0
    error_mae_ms = model_error_mae_ms(model_path)
    error_source = "контрольная MAE модели" if error_mae_ms is not None else None
    if (
        calibration_profile_path is not None
        and Path(calibration_profile_path).is_file()
    ):
        from .calibration import (
            apply_calibration,
            factor_for_query,
            load_profile,
            validate_profile,
        )

        profile = load_profile(calibration_profile_path)
        validate_profile(profile, settings, model_path)
        predicted_time_ms = apply_calibration(
            uncalibrated_time_ms, profile, sql_text
        )
        calibration_factor = factor_for_query(
            profile, sql_text, uncalibrated_time_ms
        )
        calibration_sample_count = profile.sample_count
        if profile.improves_mae and profile.calibrated_mae_ms is not None:
            error_mae_ms = profile.calibrated_mae_ms
            error_source = (
                f"локальная калибровка, {profile.unique_query_count} разных SQL"
            )

    return QueryTimePrediction(
        sql_text=sql_text.strip(),
        predicted_time_ms=predicted_time_ms,
        estimated_total_cost=estimated_plan.features.estimated_total_cost,
        estimated_plan_rows=estimated_plan.features.estimated_plan_rows,
        root_node_type=estimated_plan.features.root_node_type,
        plan_node_count=estimated_plan.features.node_count,
        uncalibrated_time_ms=uncalibrated_time_ms,
        calibration_factor=calibration_factor,
        calibration_sample_count=calibration_sample_count,
        error_mae_ms=error_mae_ms,
        error_source=error_source,
    )

