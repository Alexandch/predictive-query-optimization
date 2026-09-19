"""Single-call application service combining XGBoost, DQN and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DatabaseSettings
from .database_features import collect_database_features
from .dqn import dqn_action_encoding_version, predict_action_values
from .dqn_features import (
    collect_action_database_context,
    encode_action,
    encode_query_state,
    GENERIC_V3_ACTION_ENCODING,
)
from .explain import collect_explain
from .index_actions import IndexAction, generate_index_actions
from .prediction import QueryTimePrediction, model_error_mae_ms
from .recommendation import IndexRecommendation
from .repository import save_analysis, save_optimization_result
from .sql_features import extract_sql_features
from .strategy import build_strategy_features, predict_strategy
from .structural_advisor import StructuralRecommendation, analyze_query_structure
from .structural_feedback import save_structural_recommendations
from .training import predict_query_time


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    prediction: QueryTimePrediction
    recommendation: IndexRecommendation | None
    recommendation_threshold_ms: float
    query_run_id: int | None
    strategy_prediction: dict | None = None
    structural_recommendations: tuple[StructuralRecommendation, ...] = ()


def analyze_query(
    sql_text: str,
    xgboost_model_path: str | Path,
    dqn_model_path: str | Path,
    settings: DatabaseSettings | None = None,
    *,
    recommendation_threshold_ms: float = 50.0,
    persist: bool = True,
    calibration_profile_path: str | Path | None = None,
    strategy_model_path: str | Path | None = None,
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
    uncalibrated_time = predicted_time
    calibration_factor = 1.0
    calibration_sample_count = 0
    error_mae_ms = model_error_mae_ms(xgboost_model_path)
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
        validate_profile(profile, settings, xgboost_model_path)
        predicted_time = apply_calibration(predicted_time, profile, normalized_sql)
        calibration_factor = factor_for_query(
            profile, normalized_sql, uncalibrated_time
        )
        calibration_sample_count = profile.sample_count
        if profile.improves_mae and profile.calibrated_mae_ms is not None:
            error_mae_ms = profile.calibrated_mae_ms
            error_source = (
                f"локальная калибровка, {profile.unique_query_count} разных SQL"
            )
    prediction = QueryTimePrediction(
        sql_text=normalized_sql,
        predicted_time_ms=predicted_time,
        estimated_total_cost=estimated_plan.features.estimated_total_cost,
        estimated_plan_rows=estimated_plan.features.estimated_plan_rows,
        root_node_type=estimated_plan.features.root_node_type,
        plan_node_count=estimated_plan.features.node_count,
        uncalibrated_time_ms=uncalibrated_time,
        calibration_factor=calibration_factor,
        calibration_sample_count=calibration_sample_count,
        error_mae_ms=error_mae_ms,
        error_source=error_source,
    )
    structural_recommendations = analyze_query_structure(
        normalized_sql,
        plan_json=estimated_plan.plan_json,
        predicted_time_ms=predicted_time,
    )

    query_state = encode_query_state(feature_values)
    actions = None
    action_contexts = {}
    strategy_prediction = None
    if strategy_model_path is not None and Path(strategy_model_path).is_file():
        actions = generate_index_actions(
            normalized_sql,
            allowed_schemas=settings.allowed_schemas,
        )
        import psycopg

        with psycopg.connect(**settings.connection_kwargs()) as connection:
            action_contexts = {
                action: collect_action_database_context(
                    action, connection=connection
                )
                for action in actions[1:]
            }
        eligible_index_count = sum(
            not context["exact_index_exists"]
            and not context["prefix_index_exists"]
            for context in action_contexts.values()
        )
        strategy_prediction = predict_strategy(
            strategy_model_path,
            build_strategy_features(
                normalized_sql,
                query_state,
                eligible_index_count,
            ),
        )

    recommendation = None
    if predicted_time >= recommendation_threshold_ms:
        actions = actions or generate_index_actions(
            normalized_sql, allowed_schemas=settings.allowed_schemas
        )
        encoding_version = dqn_action_encoding_version(dqn_model_path)
        encoded_actions = []
        for action in actions:
            database_context = None
            if encoding_version == GENERIC_V3_ACTION_ENCODING:
                database_context = action_contexts.get(action)
                if database_context is None:
                    database_context = collect_action_database_context(
                        action, settings=settings
                    )
            encoded_actions.append(
                encode_action(
                    action,
                    normalized_sql,
                    encoding_version=encoding_version,
                    database_context=database_context,
                )
            )
        values = predict_action_values(
            dqn_model_path,
            query_state,
            encoded_actions,
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
        if structural_recommendations:
            structural_recommendations = save_structural_recommendations(
                query_run_id,
                structural_recommendations,
                settings=settings,
            )

    return QueryAnalysis(
        prediction=prediction,
        recommendation=recommendation,
        recommendation_threshold_ms=recommendation_threshold_ms,
        query_run_id=query_run_id,
        strategy_prediction=strategy_prediction,
        structural_recommendations=structural_recommendations,
    )
