"""Shared read-only collector: estimated model inputs and separate runtime targets."""

from datetime import datetime, timezone

import pandas as pd

from .database_features import collect_database_features
from .explain import collect_explain, measure_query_execution
from .sql_features import extract_sql_features
from .training import TARGET_COLUMN


def collect_record(case, settings):
    record = {**case, "created_utc": datetime.now(timezone.utc).isoformat()}
    plan = collect_explain(case["sql"], settings=settings, analyze=False)
    values = {**extract_sql_features(case["sql"]).as_dict(), **plan.features.as_dict(),
              **collect_database_features(case["sql"], plan.features.estimated_plan_rows, settings=settings).as_dict()}
    for name in (TARGET_COLUMN, "shared_hit_blocks", "shared_read_blocks", "temp_read_blocks", "temp_written_blocks"):
        values.pop(name, None)
    measurement = measure_query_execution(case["sql"], settings=settings, warmup_runs=1, repetitions=3)
    record.update({"status": "ok", "features": values, "samples_ms": list(measurement.samples_ms),
                   "median_ms": measurement.median_time_ms, "last_plan_json": measurement.last_result.plan_json})
    return record


def records_frame(records):
    if not records or any(row.get("status") != "ok" or len(row.get("samples_ms", [])) != 3 for row in records):
        raise ValueError("Complete successful three-run records are required")
    return pd.DataFrame([
        {**row["features"], "template_id": row["family"], "sql_text": row["sql"],
         "split": row["split"], TARGET_COLUMN: sample}
        for row in records for sample in row["samples_ms"]])
