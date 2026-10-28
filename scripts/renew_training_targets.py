"""Refresh targets in one recorded environment, leaving all controls out of fitting."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random

import pandas as pd
from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.database_features import collect_database_features
from pqo.explain import collect_explain, measure_query_execution
from pqo.measurement_environment import capture_environment
from pqo.paired_regression import cluster_interval
from pqo.paired_workload import paired_workload
from pqo.renewal_protocol import select_cases
from pqo.sql_features import extract_sql_features
from pqo.training import TARGET_COLUMN, train_xgboost
from complex_aggregation_experiment import aggregate, evaluate


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(sql):
    return parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/renewed-targets-20261027"))
    parser.add_argument("--run", action="store_true", help="Otherwise only freeze SQL and environment")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    source_path = Path("artifacts/structural-sql-v3-20261027/training.csv")
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    old_candidate = Path("artifacts/plan-flow-experiment-20261027/plan-v1/xgboost_query_time.joblib")
    source = pd.read_csv(source_path)
    protected = set()
    for name in ("window-cte-development", "complex-aggregation-development-v2",
                 "broad-first-prediction-20261027-v2", "structural-sql-v3-20261027",
                 "plan-flow-experiment-20261027", "paired-regression-20261027"):
        path = Path("artifacts") / name / ("split-manifest.json" if name == "window-cte-development" else "manifest.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload if isinstance(payload, list) else payload["cases"]:
            if item.get("split") != "train":
                protected.add(identity(item["sql"]))
    eligible = source[~source.sql_text.map(identity).isin(protected)]
    cases = select_cases(eligible, set())
    existing = {identity(sql) for sql in source.sql_text.unique()} | protected
    fresh = list(paired_workload(bounds=(4100, 17000, 51000)))
    if len({identity(case.sql_text) for case in fresh}) != 36:
        raise ValueError("Duplicate fresh control SQL")
    if any(identity(case.sql_text) in existing for case in fresh):
        raise ValueError("Fresh SQL must not overlap historical training/controls")
    cases.extend({"family": case.template_id, "sql": case.sql_text, "split": "fresh_control"} for case in fresh)
    random.Random(27102026).shuffle(cases)
    for index, case in enumerate(cases):
        case["id"] = index
    if len({identity(case["sql"]) for case in cases}) != len(cases):
        raise ValueError("SQL overlap between renewal splits")
    settings = DatabaseSettings.from_env()
    environment = capture_environment(settings)
    inputs = {str(path): sha(path) for path in (source_path, baseline, old_candidate)}
    protocol = {
        "inputs": inputs, "environment": environment, "cases": cases,
        "implementation_sha256": {str(path): sha(path) for path in (
            Path(__file__), Path("src/pqo/explain.py"), Path("src/pqo/training.py"),
            Path("src/pqo/plan_features.py"), Path("src/pqo/sql_features.py"),
            Path("src/pqo/renewal_protocol.py"), Path("src/pqo/measurement_environment.py"))},
        "historical_targets_used_for_fitting": False, "old_control_sql_used_for_fitting": False,
        "models": ["sql-v2", "plan-v1"], "target_transform": "identity",
        "template_balanced": True, "tune": False,
        "selection": "parameter_control MAE only; fresh_control evaluated after selection",
        "fresh_forms_previously_seen": True, "automatic_promotion": False,
        "criteria": {"fresh_mae_ratio_max": .9, "fresh_q_ratio_max": 1.,
                     "parameter_mae_ratio_max": 1.1, "parameter_q_ratio_max": 1.05,
                     "parameter_p90_ratio_max": 1.1, "bootstrap_upper_negative": True},
    }
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "manifest.json"
    if manifest.exists():
        stored = json.loads(manifest.read_text(encoding="utf-8"))
        if stored != protocol:
            raise ValueError("Protocol or environment changed; use a new experiment")
    else:
        manifest.write_text(json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Frozen cases:", pd.Series([case["split"] for case in cases]).value_counts().to_dict(), flush=True)
    if not args.run:
        return
    checkpoint = args.output / "measurements.json"
    if checkpoint.exists() and not args.resume:
        raise FileExistsError("Use --resume to append missing measurements, not rerun completed SQL")
    records = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else []
    done = {record["id"] for record in records}
    if len(done) != len(records) or not done <= set(range(len(cases))):
        raise ValueError("Invalid measurement checkpoint")
    for record in records:
        if any(record[key] != cases[record["id"]][key] for key in ("sql", "family", "split")):
            raise ValueError("Checkpoint case mismatch")
    for number, case in enumerate(cases, 1):
        if case["id"] in done:
            continue
        if number % 20 == 1 and capture_environment(settings)["settings"] != environment["settings"]:
            raise ValueError("PostgreSQL settings changed during measurement")
        record = {**case, "created_utc": datetime.now(timezone.utc).isoformat()}
        try:
            plan = collect_explain(case["sql"], settings=settings, analyze=False)
            features = {**extract_sql_features(case["sql"]).as_dict(), **plan.features.as_dict(),
                        **collect_database_features(case["sql"], plan.features.estimated_plan_rows, settings=settings).as_dict()}
            # Runtime-only values are neither inputs nor invented per-run buffer statistics.
            for name in (TARGET_COLUMN, "shared_hit_blocks", "shared_read_blocks", "temp_read_blocks", "temp_written_blocks"):
                features.pop(name, None)
            measurement = measure_query_execution(case["sql"], settings=settings, warmup_runs=1, repetitions=3)
            record.update({"status": "ok", "features": features, "samples_ms": list(measurement.samples_ms),
                           "median_ms": measurement.median_time_ms,
                           "last_plan_json": measurement.last_result.plan_json})
        except Exception as exc:
            record.update({"status": "error", "error_type": type(exc).__name__})
        records.append(record)
        checkpoint.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        print(f"{len(records)}/{len(cases)} {case['split']} {case['family']}: {record.get('median_ms', record.get('error_type'))}", flush=True)
    after = capture_environment(settings)
    (args.output / "environment-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    if after["settings"] != environment["settings"] or any(sha(Path(path)) != value for path, value in inputs.items()):
        raise ValueError("Settings or frozen inputs changed during experiment")
    if len(records) != len(cases) or any(record["status"] != "ok" for record in records):
        (args.output / "report.json").write_text(json.dumps({"complete": False, "models_fitted": False,
            "failed_cases": [record["id"] for record in records if record["status"] != "ok"]}), encoding="utf-8")
        raise RuntimeError("Incomplete renewal; no training on a filtered successful subset")
    rows = [{**record["features"], "template_id": record["family"], "sql_text": record["sql"],
             "split": record["split"], TARGET_COLUMN: sample}
            for record in records for sample in record["samples_ms"]]
    frame = pd.DataFrame(rows)
    frame.to_csv(args.output / "all-measurements.csv", index=False)
    train = frame[frame.split == "train"].drop(columns="split")
    dataset = args.output / "training.csv"
    train.to_csv(dataset, index=False)
    parameter = aggregate(frame[frame.split == "parameter_control"].drop(columns="split"))
    fresh_frame = aggregate(frame[frame.split == "fresh_control"].drop(columns="split"))
    candidates = {}
    for version in protocol["models"]:
        destination = args.output / version
        train_xgboost(dataset, destination, tune=False, target_transform="identity", template_balanced=True, feature_version=version)
        candidates[version] = destination / "xgboost_query_time.joblib"
    validation = {version: evaluate(path, parameter) for version, path in candidates.items()}
    selected = min(candidates, key=lambda version: validation[version]["mae_ms"])
    # Persist the selected version before inspecting any fresh-control predictions.
    (args.output / "selection.json").write_text(json.dumps({"selected": selected, "parameter_control": validation}, indent=2), encoding="utf-8")
    final = {"baseline": evaluate(baseline, fresh_frame), "previous_plan_v1": evaluate(old_candidate, fresh_frame),
             "selected": evaluate(candidates[selected], fresh_frame)}
    parameter_results = {"baseline": evaluate(baseline, parameter), **validation}
    paired = [{"family": old["template"], "actual_ms": old["actual_ms"],
               "baseline_ms": old["predicted_ms"], "candidate_ms": new["predicted_ms"]}
              for old, new in zip(final["baseline"]["points"], final["selected"]["points"])]
    interval = cluster_interval(paired)
    old, new = final["baseline"], final["selected"]
    pv_old, pv_new = parameter_results["baseline"], validation[selected]
    import numpy as np
    p90 = lambda result: float(np.quantile([abs(point["predicted_ms"] - point["actual_ms"]) for point in result["points"]], .9))
    criteria = protocol["criteria"]
    checks = {"fresh": new["mae_ms"] <= old["mae_ms"] * criteria["fresh_mae_ratio_max"] and
              new["median_q_error"] <= old["median_q_error"] and new["within_20_percent"] >= old["within_20_percent"],
              "parameter": pv_new["mae_ms"] <= pv_old["mae_ms"] * criteria["parameter_mae_ratio_max"] and
              pv_new["median_q_error"] <= pv_old["median_q_error"] * criteria["parameter_q_ratio_max"] and
              p90(pv_new) <= p90(pv_old) * criteria["parameter_p90_ratio_max"],
              "fresh_interval": interval["upper_95_ms"] < 0}
    report = {"complete": True, "training_queries": train.sql_text.nunique(), "training_families": train.template_id.nunique(),
              "selected": selected, "parameter_control": parameter_results, "fresh_control": final,
              "paired_interval": interval, "checks": checks, "external_quality_pass": all(checks.values()),
              "primary_replaced": False, "previous_internal_gate_overridden": False}
    if any(sha(Path(path)) != value for path, value in inputs.items()):
        raise ValueError("Frozen inputs changed during model fitting")
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for split in ("parameter_control", "fresh_control"):
        print(split, {name: {key: value for key, value in result.items() if key != "points"}
                      for name, result in report[split].items()}, flush=True)
    print("selected", selected, "checks", checks, "primary_replaced", False, flush=True)


if __name__ == "__main__":
    main()
