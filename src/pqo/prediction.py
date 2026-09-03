"""UI-independent query-time prediction service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DatabaseSettings
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


def predict_sql_query(
    sql_text: str,
    model_path: str | Path,
    settings: DatabaseSettings | None = None,
) -> QueryTimePrediction:
    """Predict without executing the user's query."""
    sql_features = extract_sql_features(sql_text)
    estimated_plan = collect_explain(sql_text, settings=settings, analyze=False)
    feature_values = {
        **sql_features.as_dict(),
        **estimated_plan.features.as_dict(),
    }
    predicted_time_ms = predict_query_time(model_path, feature_values)

    return QueryTimePrediction(
        sql_text=sql_text.strip(),
        predicted_time_ms=predicted_time_ms,
        estimated_total_cost=estimated_plan.features.estimated_total_cost,
        estimated_plan_rows=estimated_plan.features.estimated_plan_rows,
        root_node_type=estimated_plan.features.root_node_type,
        plan_node_count=estimated_plan.features.node_count,
    )

