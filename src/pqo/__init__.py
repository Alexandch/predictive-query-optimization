"""Predictive query optimization core package."""

from .explain import ExplainResult, collect_explain
from .plan_features import PlanFeatures, extract_plan_features
from .prediction import QueryTimePrediction, predict_sql_query
from .query_case import QueryCase
from .query_generator import AviationQueryGenerator
from .sql_features import SQLFeatures, extract_sql_features

__all__ = [
    "ExplainResult",
    "PlanFeatures",
    "QueryTimePrediction",
    "QueryCase",
    "SQLFeatures",
    "AviationQueryGenerator",
    "collect_explain",
    "extract_plan_features",
    "extract_sql_features",
    "predict_sql_query",
]
