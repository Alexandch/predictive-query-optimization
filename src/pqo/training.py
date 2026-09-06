"""Reproducible XGBoost training for PostgreSQL query-time prediction."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


TARGET_COLUMN = "actual_total_time_ms"
GROUP_COLUMN = "template_id"
CATEGORICAL_FEATURES = ["root_node_type"]
NUMERIC_FEATURES = [
    "query_length",
    "join_count",
    "where_condition_count",
    "subquery_count",
    "has_group_by",
    "has_order_by",
    "has_distinct",
    "table_reference_count",
    "unique_table_count",
    "select_expression_count",
    "aggregate_function_count",
    "inner_join_count",
    "left_join_count",
    "right_join_count",
    "full_join_count",
    "cross_join_count",
    "estimated_startup_cost",
    "estimated_total_cost",
    "estimated_plan_rows",
    "estimated_plan_width",
    "estimated_rows_all_nodes",
    "node_count",
    "max_plan_depth",
    "relation_count",
    "seq_scan_count",
    "index_scan_count",
    "index_only_scan_count",
    "bitmap_heap_scan_count",
    "hash_join_count",
    "merge_join_count",
    "nested_loop_count",
    "sort_node_count",
    "aggregate_node_count",
    "relation_row_estimate_sum",
    "largest_relation_rows",
    "relation_size_bytes",
    "index_size_bytes",
    "existing_index_count",
    "estimated_selectivity",
]
MODEL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    train_count: int
    test_count: int
    train_templates: list[str]
    test_templates: list[str]
    mae_ms: float
    median_ae_ms: float
    rmse_ms: float
    r2: float
    mape_percent: float
    within_20_percent: float


@dataclass(frozen=True, slots=True)
class TrainingMetrics:
    sample_count: int
    modeled_sample_count: int
    unique_query_count: int
    target_transform: str
    best_parameters: dict[str, int | float]
    tuning_cv_mae_ms: float | None
    parameter_holdout: EvaluationMetrics
    unseen_template_stress: EvaluationMetrics
    template_balanced: bool

    @property
    def mae_ms(self) -> float:
        return self.parameter_holdout.mae_ms


def _validate_dataset(frame) -> None:
    required = set(MODEL_FEATURES + [TARGET_COLUMN, GROUP_COLUMN, "sql_text"])
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Dataset is missing columns: {', '.join(missing)}")
    if len(frame) < 100:
        raise ValueError("At least 100 samples are required for training")
    if frame[GROUP_COLUMN].nunique() < 4:
        raise ValueError("At least four query templates are required for grouped evaluation")
    if frame["sql_text"].nunique() < 20:
        raise ValueError("At least 20 unique SQL queries are required for evaluation")
    if frame[TARGET_COLUMN].isna().any() or (frame[TARGET_COLUMN] < 0).any():
        raise ValueError("Target values must be non-negative and must not contain nulls")


def train_xgboost(
    dataset_path: str | Path,
    output_dir: str | Path,
    random_state: int = 42,
    tune: bool = True,
    target_transform: str = "log1p",
    template_balanced: bool = False,
) -> TrainingMetrics:
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.base import clone
    from sklearn.metrics import (
        make_scorer,
        mean_absolute_error,
        median_absolute_error,
        r2_score,
    )
    from sklearn.model_selection import GroupKFold, GroupShuffleSplit, RandomizedSearchCV
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder
    from xgboost import XGBRegressor

    frame = pd.read_csv(dataset_path)
    _validate_dataset(frame)
    if target_transform not in {"log1p", "identity"}:
        raise ValueError("target_transform must be 'log1p' or 'identity'")

    transform_target = np.log1p if target_transform == "log1p" else lambda values: values
    inverse_target = np.expm1 if target_transform == "log1p" else lambda values: values

    raw_sample_count = len(frame)
    aggregation = {feature: "first" for feature in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    frame = (
        frame.groupby([GROUP_COLUMN, "sql_text"], as_index=False, sort=False)
        .agg(aggregation)
    )

    x = frame[MODEL_FEATURES].copy()
    x[NUMERIC_FEATURES] = x[NUMERIC_FEATURES].apply(pd.to_numeric)
    y = frame[TARGET_COLUMN].astype(float).to_numpy()
    template_groups = frame[GROUP_COLUMN].astype(str)
    query_groups = frame["sql_text"].astype(str)
    template_counts = template_groups.value_counts()
    sample_weights = template_groups.map(
        lambda template: len(frame) / (len(template_counts) * template_counts[template])
    ).to_numpy(dtype=float)

    def fit_parameters(indices=None) -> dict[str, Any]:
        if not template_balanced:
            return {}
        weights = sample_weights if indices is None else sample_weights[indices]
        return {"regressor__sample_weight": weights}

    preprocessor = ColumnTransformer(
        transformers=[
            (
                "root_node",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CATEGORICAL_FEATURES,
            ),
            ("numeric", "passthrough", NUMERIC_FEATURES),
        ]
    )
    regressor = XGBRegressor(
        objective="reg:squarederror",
        n_estimators=500,
        max_depth=6,
        learning_rate=0.04,
        min_child_weight=3,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=1.0,
        random_state=random_state,
        n_jobs=4,
    )
    pipeline_template = Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("regressor", regressor),
        ]
    )

    parameter_splitter = GroupShuffleSplit(
        n_splits=1,
        test_size=0.20,
        random_state=random_state,
    )
    parameter_train, parameter_test = next(
        parameter_splitter.split(x, y, groups=query_groups)
    )

    selected_template = pipeline_template
    tuning_cv_mae_ms = None
    best_parameters: dict[str, int | float] = {
        "n_estimators": 500,
        "max_depth": 6,
        "learning_rate": 0.04,
        "min_child_weight": 3,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
    }
    if tune:
        def millisecond_mae(y_true_log, y_predicted_log):
            return mean_absolute_error(
                inverse_target(y_true_log),
                np.maximum(0.0, inverse_target(y_predicted_log)),
            )

        search = RandomizedSearchCV(
            estimator=pipeline_template,
            param_distributions={
                "regressor__n_estimators": [300, 500, 700, 900],
                "regressor__max_depth": [3, 4, 5, 6, 8],
                "regressor__learning_rate": [0.02, 0.04, 0.07, 0.10],
                "regressor__min_child_weight": [1, 3, 6, 10],
                "regressor__subsample": [0.70, 0.85, 1.0],
                "regressor__colsample_bytree": [0.70, 0.85, 1.0],
            },
            n_iter=20,
            scoring=make_scorer(millisecond_mae, greater_is_better=False),
            cv=GroupKFold(n_splits=5),
            refit=True,
            random_state=random_state,
            n_jobs=1,
            verbose=0,
        )
        search.fit(
            x.iloc[parameter_train],
            transform_target(y[parameter_train]),
            groups=query_groups.iloc[parameter_train],
            **fit_parameters(parameter_train),
        )
        selected_template = search.best_estimator_
        tuning_cv_mae_ms = float(-search.best_score_)
        best_parameters = {
            name.removeprefix("regressor__"): value
            for name, value in search.best_params_.items()
        }

    def evaluate(train_indices, test_indices) -> EvaluationMetrics:
        evaluation_pipeline = clone(selected_template)
        x_train, x_test = x.iloc[train_indices], x.iloc[test_indices]
        y_train, y_test = y[train_indices], y[test_indices]
        evaluation_pipeline.fit(
            x_train,
            transform_target(y_train),
            **fit_parameters(train_indices),
        )
        predictions = np.maximum(
            0.0,
            inverse_target(evaluation_pipeline.predict(x_test)),
        )
        absolute_errors = np.abs(y_test - predictions)
        relative_errors = absolute_errors / np.maximum(y_test, 0.001)
        return EvaluationMetrics(
            train_count=len(train_indices),
            test_count=len(test_indices),
            train_templates=sorted(
                template_groups.iloc[train_indices].unique().tolist()
            ),
            test_templates=sorted(
                template_groups.iloc[test_indices].unique().tolist()
            ),
            mae_ms=float(mean_absolute_error(y_test, predictions)),
            median_ae_ms=float(median_absolute_error(y_test, predictions)),
            rmse_ms=float(np.sqrt(np.mean((y_test - predictions) ** 2))),
            r2=float(r2_score(y_test, predictions)),
            mape_percent=float(np.mean(relative_errors) * 100),
            within_20_percent=float(np.mean(relative_errors <= 0.20) * 100),
        )

    template_splitter = GroupShuffleSplit(
        n_splits=1,
        test_size=0.25,
        random_state=random_state,
    )
    template_train, template_test = next(
        template_splitter.split(x, y, groups=template_groups)
    )
    metrics = TrainingMetrics(
        sample_count=raw_sample_count,
        modeled_sample_count=len(frame),
        unique_query_count=int(frame["sql_text"].nunique()),
        target_transform=target_transform,
        best_parameters=best_parameters,
        tuning_cv_mae_ms=tuning_cv_mae_ms,
        parameter_holdout=evaluate(parameter_train, parameter_test),
        unseen_template_stress=evaluate(template_train, template_test),
        template_balanced=template_balanced,
    )

    pipeline = clone(selected_template)
    pipeline.fit(x, transform_target(y), **fit_parameters())

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    artifact: dict[str, Any] = {
        "pipeline": pipeline,
        "model_features": MODEL_FEATURES,
        "target_column": TARGET_COLUMN,
        "target_transform": target_transform,
        "requires_query_execution": False,
        "random_state": random_state,
        "template_balanced": template_balanced,
    }
    joblib.dump(artifact, destination / "xgboost_query_time.joblib")
    (destination / "metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    feature_names = pipeline.named_steps["preprocessor"].get_feature_names_out()
    importances = pipeline.named_steps["regressor"].feature_importances_
    ranked = sorted(
        (
            {"feature": str(name), "importance": float(importance)}
            for name, importance in zip(feature_names, importances, strict=True)
        ),
        key=lambda item: item["importance"],
        reverse=True,
    )
    (destination / "feature_importance.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metrics


def predict_query_time(model_path: str | Path, features: dict[str, Any]) -> float:
    import joblib
    import numpy as np
    import pandas as pd

    artifact = joblib.load(model_path)
    missing = sorted(set(artifact["model_features"]).difference(features))
    if missing:
        raise ValueError(f"Prediction features are missing: {', '.join(missing)}")

    frame = pd.DataFrame([{name: features[name] for name in artifact["model_features"]}])
    prediction = artifact["pipeline"].predict(frame)[0]
    if artifact["target_transform"] == "log1p":
        prediction = np.expm1(prediction)
    return float(max(0.0, prediction))
