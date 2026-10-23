"""Read-only/rolled-back demonstration, not model training or benchmark tuning."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
import psycopg
from pqo.config import DatabaseSettings
from pqo.sql_rewrite import evaluate_sql_rewrites
from pqo.sequential_recommendation import recommend_sequential_indexes

INDEX_SQL = """SELECT tracking_event_id, shipment_id, event_type, event_time
FROM logistics.tracking_events
WHERE facility_id = 17 AND event_type = 'exception'
ORDER BY event_time DESC
LIMIT 200"""

REWRITE_SQL = """SELECT s.shipment_id, s.tracking_code, s.shipment_status
FROM logistics.shipments AS s
WHERE s.shipment_id BETWEEN 1000 AND 1049
  AND (SELECT COUNT(*) FROM logistics.tracking_events AS te
       WHERE te.shipment_id = s.shipment_id) > 0
ORDER BY s.shipment_id"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        print(connection.execute("SELECT schemaname,relname,n_live_tup FROM pg_stat_user_tables WHERE schemaname IN ('logistics','pagila') ORDER BY n_live_tup DESC LIMIT 12").fetchall(), flush=True)
        print(connection.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='logistics' AND tablename='tracking_events'").fetchall(), flush=True)
        print("rows", connection.execute("SELECT (SELECT COUNT(*) FROM logistics.tracking_events), (SELECT COUNT(*) FROM logistics.shipments), (SELECT COUNT(*) FROM pagila.rental)").fetchone(), flush=True)
    if args.inventory:
        return
    result = {}
    plan = evaluate_sql_rewrites(REWRITE_SQL, settings, repetitions=3)
    result["rewrite"] = asdict(plan)
    print("rewrite", plan.terminal_reason, [(item.candidate.rule_id, item.baseline_time_ms, item.rewritten_time_ms, item.rejection_reason) for item in plan.evaluations], flush=True)
    indexes = recommend_sequential_indexes(
        INDEX_SQL, Path("models/sequential_dqn/sequential_dqn_index_advisor.pt"), settings,
        max_steps=2, repetitions=3, minimum_baseline_time_ms=5,
        minimum_absolute_improvement_ms=2, minimum_improvement_ratio=.05, persist=False,
    )
    result["index"] = asdict(indexes)
    print("index", indexes.terminal_reason, indexes.baseline_time_ms, indexes.final_time_ms, flush=True)
    directory = Path("artifacts/rewrite-preview-verification")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "examples.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
