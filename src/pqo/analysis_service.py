"""Single-call application service combining XGBoost, DQN and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DatabaseSettings
from .database_features import collect_database_features
from .dqn import predict_action_values
from .dqn_features import encode_action, encode_query_state
from .explain import collect_explain
from .index_actions import IndexAction, generate_index_actions
from .prediction import QueryTimePrediction
from .recommendation import IndexRecommendation
from .repository import save_analysis, save_optimization_result
from .sql_features import extract_sql_features
from .training import predict_query_time


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    prediction: QueryTimePrediction
    recommendation: IndexRecommendation | None
    recommendation_threshold_ms: float
    query_run_id: int | None


def analyze_query(
    sql_text: str,
    xgboost_model_path: str | Path,
    dqn_model_path: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    recommendation_threshold_ms: float = 50.0,
    persist: bool = True,
) -> QueryAnalysis:
    if recommendation_threshold_ms < 0:
        raise ValueError("recommendation_threshold_ms must be non-negative")
    settings = settings or DatabaseSettings.from_env()
    normalized_sql = sql_text.strip()
    sql_features = extract_sql_features(normalized_sql)
    estimated_plan = collect_explain(normalized_sql, settings=settings, analyze=False)
    database_features = collect_database_features(
        normalized_sql,
        estimated_plan.features.estimated_plan_rows,
        settings=settings,
    )
    feature_values = {
        **sql_features.as_dict(),
        **estimated_plan.features.as_dict(),
        **database_features.as_dict(),
    }
    predicted_time = predict_query_time(xgboost_model_path, feature_values)
    prediction = QueryTimePrediction(
        sql_text=normalized_sql,
        predicted_time_ms=predicted_time,
        estimated_total_cost=estimated_plan.features.estimated_total_cost,
        estimated_plan_rows=estimated_plan.features.estimated_plan_rows,
        root_node_type=estimated_plan.features.root_node_type,
        plan_node_count=estimated_plan.features.node_count,
    )

    recommendation = None
    if predicted_time >= recommendation_threshold_ms:
        actions = generate_index_actions(normalized_sql)
        values = predict_action_values(
            dqn_model_path,
            encode_query_state(feature_values),
            [encode_action(action, normalized_sql) for action in actions],
        )
        best_index = max(range(len(actions)), key=values.__getitem__)
        if best_index != 0 and values[best_index] <= 0:
            best_index = 0
        recommendation = IndexRecommendation(
            action=actions[best_index],
            predicted_reward=values[best_index],
            candidate_count=len(actions),
        )

    query_run_id = None
    if persist:
        query_run_id = save_analysis(
            normalized_sql,
            estimated_plan.plan_json,
            sql_features,
            estimated_plan.features,
            database_features,
            settings=settings,
            source="application",
        )
        save_optimization_result(
            query_run_id,
            predicted_time,
            recommendation,
            settings=settings,
        )

    return QueryAnalysis(
        prediction=prediction,
        recommendation=recommendation,
        recommendation_threshold_ms=recommendation_threshold_ms,
        query_run_id=query_run_id,
    )
