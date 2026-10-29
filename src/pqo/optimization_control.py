"""Fixed local diagnostic matrix; never trains models or deploys indexes."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import monotonic

from .config import DatabaseSettings
from .sequential_recommendation import recommend_sequential_indexes
from .sql_rewrite import evaluate_sql_rewrites


@dataclass(frozen=True)
class ControlCase:
    name: str
    schema: str
    mode: str
    intent: str
    sql: str


def control_cases() -> tuple[ControlCase, ...]:
    cases = [
        ControlCase("aviation-index", "aviation", "index", "selective filter and ordering",
            "SELECT flight_id, scheduled_departure FROM aviation.flights "
            "WHERE departure_airport='MSQ' ORDER BY scheduled_departure LIMIT 200"),
        ControlCase("retail-index", "retail", "index", "selective customer filter",
            "SELECT order_id, ordered_at, total_amount FROM retail.customer_orders "
            "WHERE customer_id=17 ORDER BY ordered_at DESC LIMIT 100"),
        ControlCase("logistics-index", "logistics", "index", "selective event filter",
            "SELECT tracking_event_id,shipment_id,event_type,event_time FROM logistics.tracking_events "
            "WHERE facility_id=17 AND event_type='exception' ORDER BY event_time DESC LIMIT 200"),
    ]
    for schema, parent, key, child, child_key in (
        ("aviation", "flights", "flight_id", "ticket_flights", "flight_id"),
        ("retail", "customers", "customer_id", "customer_orders", "customer_id"),
        ("logistics", "shipments", "shipment_id", "tracking_events", "shipment_id"),
    ):
        cases.append(ControlCase(f"{schema}-rewrite", schema, "rewrite", "COUNT to EXISTS and inner sort",
            f"SELECT q.{key} FROM (SELECT p.{key} FROM {schema}.{parent} p "
            f"WHERE p.{key} BETWEEN 1000 AND 1019 AND "
            f"(SELECT COUNT(*) FROM {schema}.{child} c WHERE c.{child_key}=p.{key})>0 "
            f"ORDER BY p.{key} DESC) q ORDER BY q.{key} LIMIT 20"))
        cases.append(ControlCase(f"{schema}-negative", schema, "both", "existing primary key lookup",
            f"SELECT {key} FROM {schema}.{parent} WHERE {key}=1000"))
    return tuple(cases)


def index_inventory(settings):
    import psycopg
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        return connection.execute(
            "SELECT schemaname,indexname,indexdef FROM pg_indexes "
            "WHERE schemaname=ANY(%s) ORDER BY 1,2", (sorted(settings.allowed_schemas),)
        ).fetchall()


def run_control(output: Path, model: Path, settings: DatabaseSettings, *, repetitions=3):
    """Reserve a new output directory and checkpoint every trial, including errors."""
    import psycopg
    if type(repetitions) is not int or not 1 <= repetitions <= 5:
        raise ValueError("repetitions must be between 1 and 5")
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    settings = replace(settings, allowed_schemas=frozenset({"aviation", "retail", "logistics"}))
    cases = control_cases()
    output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "purpose": "local diagnostic; not independent holdout, training or model selection",
              "model_sha256": model_hash, "repetitions": repetitions,
              "statement_timeout_ms": settings.statement_timeout_ms,
              "thresholds": {"baseline_ms": 5, "absolute_ms": 2, "relative": .05},
              "manifest": [asdict(case) for case in cases], "results": []}

    def checkpoint():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint()
    before = index_inventory(settings)
    report["index_inventory_before"] = before
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        report["database"], report["postgres_version"] = connection.execute(
            "SELECT current_database(),version()"
        ).fetchone()
    try:
        for case in cases:
            print(f"Starting {case.name}", flush=True)
            result = {"case": case.name, "status": "running"}
            report["results"].append(result)
            checkpoint()
            started = monotonic()
            try:
                if case.mode in {"index", "both"}:
                    result["index"] = asdict(recommend_sequential_indexes(
                        case.sql, model, settings, repetitions=repetitions, max_steps=1,
                        max_candidates_per_step=3, minimum_baseline_time_ms=5,
                        minimum_absolute_improvement_ms=2, minimum_improvement_ratio=.05,
                        persist=False,
                    ))
                if case.mode in {"rewrite", "both"}:
                    result["rewrite"] = asdict(evaluate_sql_rewrites(
                        case.sql, settings, repetitions=repetitions, minimum_baseline_time_ms=5,
                        minimum_absolute_improvement_ms=2, minimum_improvement_ratio=.05,
                        journal_directory=output / "rewrite-journal", journal_source="test",
                        journal_metadata={"case_id": case.name, "diagnostic": True},
                    ))
                result["status"] = "completed"
            except Exception as exc:
                result.update(status="error", error_type=type(exc).__name__, error=str(exc))
            finally:
                result["elapsed_seconds"] = monotonic() - started
                result["index_inventory_unchanged"] = index_inventory(settings) == before
                checkpoint()
            if not result["index_inventory_unchanged"]:
                raise RuntimeError("Index inventory changed; stopped without deleting any indexes")
            print(f"Finished {case.name}: {result['status']}", flush=True)
        report["status"] = ("completed_with_errors" if any(
            result["status"] == "error" for result in report["results"]) else "completed")
    except BaseException:
        report["status"] = "interrupted"
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["index_inventory_unchanged"] = index_inventory(settings) == before
        report["model_unchanged"] = hashlib.sha256(model.read_bytes()).hexdigest() == model_hash
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path("models/sequential_dqn/sequential_dqn_index_advisor.pt"))
    parser.add_argument("--timeout-ms", type=int, default=8000)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.timeout_ms <= 0:
        parser.error("timeout-ms must be positive")
    result = run_control(args.output, args.model,
        replace(DatabaseSettings.from_env(), statement_timeout_ms=args.timeout_ms),
        repetitions=args.repetitions)
    print(result["status"], args.output / "report.json", flush=True)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
