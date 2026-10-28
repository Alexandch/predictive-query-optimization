"""Verify one model-selected index plan; all trial DDL must be rolled back."""

from dataclasses import asdict
import argparse
import json
from pathlib import Path

import psycopg
from pqo.config import DatabaseSettings
from pqo.desktop import _format_index_impact, _format_index_candidate_checks
from pqo.sequential_recommendation import recommend_sequential_indexes


def inventory(settings):
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        return connection.execute("SELECT schemaname,indexname,indexdef FROM pg_indexes WHERE schemaname=ANY(%s) ORDER BY 1,2",
                                  (sorted(settings.allowed_schemas),)).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", type=Path, default=Path("examples/logistics-index-tradeoffs.sql"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/index-tradeoffs-check"))
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    before = inventory(settings)
    sql = args.example.read_text(encoding="utf-8")
    plan = recommend_sequential_indexes(sql, Path("models/sequential_dqn/sequential_dqn_index_advisor.pt"),
        settings, repetitions=3, max_steps=1, minimum_baseline_time_ms=5,
        minimum_absolute_improvement_ms=2, minimum_improvement_ratio=.05, persist=False)
    if inventory(settings) != before:
        raise RuntimeError("Index inventory changed after rolled-back experiment")
    destination = args.output
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "report.json").write_text(json.dumps({"plan": asdict(plan), "index_inventory_unchanged": True,
        "write_latency_measured": False}, indent=2), encoding="utf-8")
    print(plan.terminal_reason, round(plan.baseline_time_ms, 3), "->", round(plan.final_time_ms, 3), flush=True)
    for step in plan.steps:
        print(step.action, flush=True)
        print(_format_index_impact(step), flush=True)
    print(_format_index_candidate_checks(plan), flush=True)


if __name__ == "__main__":
    main()
