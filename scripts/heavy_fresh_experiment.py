"""Expand current-environment training, never importing earlier held-out SQL."""

import argparse
import hashlib
import json
from pathlib import Path
import random

import pandas as pd
from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.explain import collect_explain
from pqo.fresh_measurements import collect_record, records_frame
from pqo.heavy_fresh_workload import heavy_fresh_workload
from pqo.ordered_workload import ordered_workload
from pqo.measurement_environment import capture_environment
from pqo.paired_regression import cluster_interval
from pqo.training import train_xgboost
from complex_aggregation_experiment import aggregate, evaluate


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(sql):
    return parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/heavy-fresh-training-20261027"))
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ordered-stage", action="store_true")
    args = parser.parse_args()
    previous = Path("artifacts/renewed-targets-20261027")
    if args.ordered_stage:
        previous = Path("artifacts/heavy-fresh-training-20261027-v3")
    base_path = previous / "training.csv"
    previous_model = previous / "plan-v1/xgboost_query_time.joblib"
    if args.ordered_stage:
        previous_model = previous / "identity/xgboost_query_time.joblib"
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    base = pd.read_csv(base_path)
    old_path = previous / ("new-measurements.csv" if args.ordered_stage else "all-measurements.csv")
    old_measurements = pd.read_csv(old_path)
    regression = aggregate(old_measurements[old_measurements.split == ("acceptance" if args.ordered_stage else "parameter_control")].drop(columns="split"))
    seen = {identity(sql) for sql in set(base.sql_text) | set(old_measurements.sql_text)}
    protected = set()
    for directory in ("window-cte-development", "complex-aggregation-development-v2",
                      "broad-first-prediction-20261027-v2", "structural-sql-v3-20261027",
                      "plan-flow-experiment-20261027", "paired-regression-20261027", "renewed-targets-20261027",
                      *(("heavy-fresh-training-20261027-v3",) if args.ordered_stage else ())):
        name = "split-manifest.json" if directory == "window-cte-development" else "manifest.json"
        payload = json.loads((Path("artifacts") / directory / name).read_text(encoding="utf-8"))
        for item in payload if isinstance(payload, list) else payload["cases"]:
            seen.add(identity(item["sql"]))
            if item.get("split") != "train":
                protected.add(identity(item["sql"]))
    if {identity(sql) for sql in base.sql_text} & protected:
        raise ValueError("Base training contains protected control SQL")
    cases = [{"split": split, "family": case.template_id, "sql": case.sql_text}
             for split, case in (ordered_workload() if args.ordered_stage else heavy_fresh_workload())]
    normalized = {identity(case["sql"]) for case in cases}
    if len(normalized) != len(cases) or normalized & seen:
        raise ValueError("Workload must be unique and absent from previous measured SQL")
    random.Random(27102027).shuffle(cases)
    for number, case in enumerate(cases):
        case["id"] = number
    settings = DatabaseSettings.from_env()
    environment = capture_environment(settings)
    previous_environment = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))["environment"]
    if environment["settings"] != previous_environment["settings"] or environment["database"] != previous_environment["database"]:
        raise ValueError("Do not mix refreshed base with a different PostgreSQL environment")
    files = {str(path): sha(path) for path in (base_path, previous_model, baseline, old_path)}
    protocol = {"files": files, "environment": environment, "cases": cases,
                "models": ["identity", "log1p"], "feature_version": "plan-v1", "tune": False,
                "selection": "new validation MAE only; never cached regression or acceptance",
                "regression_reuses_previously_seen_control": True, "automatic_promotion": False,
                "criteria": {"mae_improvement_min": .10, "regression_mae_ratio_max": 1.10,
                    "regression_q_ratio_max": 1.05, "within20_loss_max": .05,
                    "acceptance_q_not_worse": True, "bootstrap_upper_negative": True},
                "implementation_sha256": {str(path): sha(path) for path in (
                    Path(__file__), Path("src/pqo/heavy_fresh_workload.py"), Path("src/pqo/fresh_measurements.py"),
                    Path("src/pqo/explain.py"), Path("src/pqo/plan_features.py"), Path("src/pqo/training.py"))}}
    if args.ordered_stage:
        protocol["implementation_sha256"]["src/pqo/ordered_workload.py"] = sha(Path("src/pqo/ordered_workload.py"))
        protocol["workload"] = "ordered-stage-v1"
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != protocol:
            raise ValueError("Frozen protocol changed; use a new directory")
    else:
        manifest.write_text(json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Frozen:", pd.Series([case["split"] for case in cases]).value_counts().to_dict(), flush=True)
    if not args.run:
        return
    checkpoint = args.output / "measurements.json"
    if checkpoint.exists() and not args.resume:
        raise FileExistsError("Use --resume for missing cases only")
    records = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else []
    done = {row["id"] for row in records}
    if len(done) != len(records) or not done <= set(range(len(cases))):
        raise ValueError("Invalid checkpoint")
    for row in records:
        if any(row[key] != cases[row["id"]][key] for key in ("split", "family", "sql")):
            raise ValueError("Checkpoint mismatch")
    for case in cases:
        collect_explain(case["sql"], settings=settings, analyze=False)
    print("All SQL passed PostgreSQL EXPLAIN", flush=True)
    for number, case in enumerate(cases, 1):
        if case["id"] in done:
            continue
        if number % 10 == 1 and capture_environment(settings)["settings"] != environment["settings"]:
            raise ValueError("PostgreSQL settings changed")
        try:
            row = collect_record(case, settings)
        except Exception as exc:
            row = {**case, "status": "error", "error_type": type(exc).__name__}
        records.append(row)
        checkpoint.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        print(f"{len(records)}/{len(cases)} {case['family']}: {row.get('median_ms', row.get('error_type'))}", flush=True)
    after = capture_environment(settings)
    (args.output / "environment-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    if after["settings"] != environment["settings"] or any(sha(Path(path)) != value for path, value in files.items()):
        raise ValueError("Environment or frozen inputs changed")
    if len(records) != len(cases) or any(row["status"] != "ok" for row in records):
        (args.output / "report.json").write_text(json.dumps({"complete": False, "models_fitted": False}), encoding="utf-8")
        raise RuntimeError("Incomplete collection; do not fit only successful cases")
    frame = records_frame(records)
    frame.to_csv(args.output / "new-measurements.csv", index=False)
    added = frame[frame.split == "train"].drop(columns="split")
    combined = pd.concat([base, added], ignore_index=True)
    combined.to_csv(args.output / "training.csv", index=False)
    validation = aggregate(frame[frame.split == "validation"].drop(columns="split"))
    final = aggregate(frame[frame.split == "acceptance"].drop(columns="split"))
    candidates = {}
    for transform in protocol["models"]:
        destination = args.output / transform
        train_xgboost(args.output / "training.csv", destination, tune=False,
                      target_transform=transform, template_balanced=True, feature_version="plan-v1")
        candidates[transform] = destination / "xgboost_query_time.joblib"
    validation_results = {name: evaluate(path, validation) for name, path in candidates.items()}
    selected = min(candidates, key=lambda name: validation_results[name]["mae_ms"])
    (args.output / "selection.json").write_text(json.dumps({"selected": selected, "validation": validation_results}, indent=2), encoding="utf-8")
    final_results = {"production": evaluate(baseline, final), "previous_refreshed": evaluate(previous_model, final),
                     "selected": evaluate(candidates[selected], final)}
    regression_results = {"previous_refreshed": evaluate(previous_model, regression),
                          "selected": evaluate(candidates[selected], regression)}
    previous_validation = evaluate(previous_model, validation)
    paired = [{"family": old["template"], "actual_ms": old["actual_ms"], "baseline_ms": old["predicted_ms"],
               "candidate_ms": new["predicted_ms"]}
              for old, new in zip(final_results["previous_refreshed"]["points"], final_results["selected"]["points"])]
    interval = cluster_interval(paired)
    candidate_final = final_results["selected"]
    old_reg, new_reg = regression_results["previous_refreshed"], regression_results["selected"]
    checks = {"validation": validation_results[selected]["mae_ms"] <= previous_validation["mae_ms"] * .9,
              "acceptance": all(candidate_final["mae_ms"] <= old["mae_ms"] * .9 and
                  candidate_final["median_q_error"] <= old["median_q_error"] and
                  candidate_final["within_20_percent"] >= old["within_20_percent"]
                  for old in (final_results["production"], final_results["previous_refreshed"])),
              "regression": new_reg["mae_ms"] <= old_reg["mae_ms"] * 1.1 and
                  new_reg["median_q_error"] <= old_reg["median_q_error"] * 1.05 and
                  new_reg["within_20_percent"] >= old_reg["within_20_percent"] - .05,
              "paired_interval": interval["upper_95_ms"] < 0}
    report = {"complete": True, "selected": selected, "training_queries": combined.sql_text.nunique(),
              "training_families": combined.template_id.nunique(), "validation": {"previous_refreshed": previous_validation, **validation_results},
              "acceptance": final_results, "previously_seen_regression": regression_results,
              "paired_interval": interval, "checks": checks, "external_quality_pass": all(checks.values()), "primary_replaced": False}
    if any(sha(Path(path)) != value for path, value in files.items()):
        raise ValueError("Frozen inputs changed during training")
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for split in ("validation", "acceptance", "previously_seen_regression"):
        print(split, {name: {key: value for key, value in result.items() if key != "points"}
                      for name, result in report[split].items()}, flush=True)
    print("selected", selected, "checks", checks, "primary_replaced", False, flush=True)


if __name__ == "__main__":
    main()
