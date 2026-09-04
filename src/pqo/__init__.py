"""Predictive query optimization core package."""

from .explain import ExplainResult, collect_explain
from .plan_features import PlanFeatures, extract_plan_features
from .database_features import (
    DatabaseFeatures,
    collect_database_features,
    extract_relation_references,
)
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
    "DatabaseFeatures",
    "collect_database_features",
    "extract_relation_references",
    "AviationQueryGenerator",
    "collect_explain",
    "extract_plan_features",
    "extract_sql_features",
    "predict_sql_query",
]
