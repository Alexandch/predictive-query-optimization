"""Backfill estimated plan-flow columns, rejecting changed historical plans."""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import psycopg

from pqo.config import DatabaseSettings
from pqo.explain import collect_explain
from pqo.plan_features import PLAN_FLOW_FEATURES


MATCH_FIELDS = ("estimated_total_cost", "estimated_startup_cost", "estimated_plan_rows",
                "estimated_plan_width", "node_count", "estimated_rows_all_nodes")


def matches_historical_plan(row, current):
    if str(row["root_node_type"]) != current["root_node_type"]:
        return False
    return all(abs(float(row[name]) - float(current[name])) <=
               max(0.01, abs(float(row[name])) * .01) for name in MATCH_FIELDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = replace(DatabaseSettings.from_env(), statement_timeout_ms=3000)
    protocol = {"source_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                "features": PLAN_FLOW_FEATURES, "plan_match_relative_tolerance": .01,
                "match_fields": list(MATCH_FIELDS), "analyze": False,
                "host": settings.host, "port": settings.port, "database": settings.dbname}
    manifest = args.output / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != protocol:
            raise ValueError("Backfill source/protocol changed; use a new directory")
    else:
        manifest.write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    cache_path = args.output / "estimated-flows.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    frame = pd.read_csv(args.dataset)
    queries = frame.sql_text.unique()
    with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
        for number, sql in enumerate(queries, 1):
            if sql not in cache:
                try:
                    values = collect_explain(sql, settings=settings, analyze=False, connection=connection).features.as_dict()
                    cache[sql] = {key: values[key] for key in PLAN_FLOW_FEATURES + list(MATCH_FIELDS) + ["root_node_type"]}
                except psycopg.Error as exc:
                    cache[sql] = {"error": type(exc).__name__}
            if number % 100 == 0 or number == len(queries):
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
                print(f"Estimated plans {number}/{len(queries)}", flush=True)
    keep = frame.apply(lambda row: "error" not in cache[row.sql_text] and
                      matches_historical_plan(row, cache[row.sql_text]), axis=1)
    retained = frame.loc[keep].copy()
    for name in PLAN_FLOW_FEATURES:
        retained[name] = retained.sql_text.map(lambda sql: cache[sql][name])
    retained.to_csv(args.output / "training.csv", index=False)
    report = {"source_rows": len(frame), "retained_rows": len(retained),
              "source_unique_sql": len(queries), "retained_unique_sql": retained.sql_text.nunique(),
              "failed_sql": sum("error" in value for value in cache.values()),
              "dropped_rows_by_template": frame.loc[~keep].groupby("template_id").size().to_dict(),
              "estimated_only": True, "historical_targets_remeasured": False}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
