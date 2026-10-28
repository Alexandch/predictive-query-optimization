"""Fixed combined-feature hypothesis with fresh acceptance and no automatic promotion."""

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
from pqo.measurement_environment import capture_environment
from pqo.ordered_transfer_workload import ordered_transfer_workload
from pqo.paired_regression import cluster_interval
from pqo.training import train_xgboost
from complex_aggregation_experiment import aggregate, evaluate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/ordered-features-20261027"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    source = Path("artifacts/ordered-training-20261027-v2")
    baseline = Path("models/xgboost/trial/xgboost_query_time.joblib")
    training = pd.read_csv(source / "training.csv")
    measured = pd.read_csv(source / "new-measurements.csv")
    validation = aggregate(measured[measured.split == "validation"].drop(columns="split"))
    normalize = lambda sql: parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)
    seen = {normalize(sql) for sql in set(training.sql_text) | set(measured.sql_text)}
    # Protect every earlier registered holdout, not just the last experiment.
    for name in ("window-cte-development", "complex-aggregation-development-v2",
                 "broad-first-prediction-20261027-v2", "structural-sql-v3-20261027",
                 "plan-flow-experiment-20261027", "paired-regression-20261027",
                 "renewed-targets-20261027", "heavy-fresh-training-20261027-v3"):
        path = Path("artifacts") / name / ("split-manifest.json" if name == "window-cte-development" else "manifest.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = data if isinstance(data, list) else data["cases"]
        protected = {normalize(case["sql"]) for case in cases if case.get("split") != "train"}
        if {normalize(sql) for sql in training.sql_text} & protected:
            raise ValueError("Previous holdout leaked into fit")
        seen.update(normalize(case["sql"]) for case in cases)
    cases = [{"id": i, "family": case.template_id, "split": "acceptance", "sql": case.sql_text}
             for i, case in enumerate(ordered_transfer_workload())]
    texts = {normalize(case["sql"]) for case in cases}
    if len(texts) != len(cases) or texts & seen:
        raise ValueError("Fresh acceptance overlaps earlier SQL")
    random.Random(27102028).shuffle(cases)
    settings = DatabaseSettings.from_env()
    environment = capture_environment(settings)
    prior_environment = json.loads((source / "manifest.json").read_text(encoding="utf-8"))["environment"]
    if environment["settings"] != prior_environment["settings"] or environment["database"] != prior_environment["database"]:
        raise ValueError("Measured base environment changed")
    frozen_files = {str(path): sha(path) for path in (
        source / "training.csv", source / "new-measurements.csv", baseline,
        Path("src/pqo/training.py"), Path("src/pqo/ordered_transfer_workload.py"), Path(__file__))}
    protocol = {"hypothesis": "plan-v2 adds frozen sql-v3 features to plan-v1; no new targets in fit",
                "models": ["identity", "log1p"], "selection": "previously inspected validation MAE only",
                "cases": cases, "files": frozen_files, "environment": environment,
                "criteria": {"validation_mae_ratio_max": .9, "fresh_mae_ratio_max": .9,
                    "relative_metrics_not_worse": True, "regression_mae_ratio_max": 1.1,
                    "regression_q_ratio_max": 1.05, "within20_loss_max": .05,
                    "paired_interval_upper_negative": True, "ablation_mae_not_worse": True},
                "automatic_promotion": False}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != protocol:
        raise ValueError("Frozen protocol differs")
    manifest.write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    if not args.run:
        return
    if (args.output / "report.json").exists():
        raise FileExistsError("This final control has already been evaluated")
    candidates = {}
    for transform in protocol["models"]:
        directory = args.output / transform
        train_xgboost(source / "training.csv", directory, tune=False, template_balanced=True,
                      feature_version="plan-v2", target_transform=transform)
        candidates[transform] = directory / "xgboost_query_time.joblib"
    results = {name: evaluate(path, validation) for name, path in candidates.items()}
    selected = min(results, key=lambda name: results[name]["mae_ms"])
    (args.output / "selection.json").write_text(json.dumps({"selected": selected, "validation": results}, indent=2), encoding="utf-8")
    print("Selection fixed before fresh acceptance:", selected, flush=True)
    for case in cases:
        collect_explain(case["sql"], settings=settings, analyze=False)
    checkpoint = args.output / "measurements.json"
    if checkpoint.exists():
        raise FileExistsError("Collection already started; inspect incomplete protocol before restarting")
    records = []
    for case in cases:
        try:
            record = collect_record(case, settings)
        except Exception as exc:
            record = {**case, "status": "error", "error_type": type(exc).__name__}
        records.append(record)
        checkpoint.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        print(f"{len(records)}/{len(cases)} {case['family']}: {record.get('median_ms', record.get('error_type'))}", flush=True)
    if any(record["status"] != "ok" for record in records):
        raise RuntimeError("Incomplete control; no failing SQL is dropped")
    after = capture_environment(settings)
    if after["settings"] != environment["settings"] or any(sha(path) != value for path, value in frozen_files.items()):
        raise ValueError("Frozen inputs or environment changed")
    fresh = aggregate(records_frame(records).drop(columns="split"))
    final = {"current": evaluate(baseline, fresh), "candidate": evaluate(candidates[selected], fresh),
             "same_data_plan_v1": evaluate(source / "identity/xgboost_query_time.joblib", fresh)}
    cohorts = {"last_acceptance": aggregate(measured[measured.split == "acceptance"].drop(columns="split"))}
    for name, path, split in (
        ("heavy_acceptance", "artifacts/heavy-fresh-training-20261027-v3/new-measurements.csv", "acceptance"),
        ("parameter_control", "artifacts/renewed-targets-20261027/all-measurements.csv", "parameter_control")):
        frame = pd.read_csv(path)
        cohorts[name] = aggregate(frame[frame.split == split].drop(columns="split"))
    regressions = {name: {"current": evaluate(baseline, frame), "candidate": evaluate(candidates[selected], frame)}
                   for name, frame in cohorts.items()}
    paired = [{"family": old["template"], "actual_ms": old["actual_ms"], "baseline_ms": old["predicted_ms"],
               "candidate_ms": new["predicted_ms"]} for old, new in zip(final["current"]["points"], final["candidate"]["points"])]
    interval = cluster_interval(paired)
    old, new = final["current"], final["candidate"]
    checks = {"validation": results[selected]["mae_ms"] <= evaluate(baseline, validation)["mae_ms"] * .9,
              "fresh": new["mae_ms"] <= old["mae_ms"] * .9 and new["median_q_error"] <= old["median_q_error"]
                  and new["within_20_percent"] >= old["within_20_percent"],
              "ablation": new["mae_ms"] <= final["same_data_plan_v1"]["mae_ms"],
              "paired_interval": interval["upper_95_ms"] < 0}
    for name, values in regressions.items():
        old, new = values["current"], values["candidate"]
        checks[name] = (new["mae_ms"] <= old["mae_ms"] * 1.1 and new["median_q_error"] <= old["median_q_error"] * 1.05
                        and new["within_20_percent"] >= old["within_20_percent"] - .05)
    report = {"selected": selected, "validation": results, "fresh_acceptance": final,
              "previously_seen_regression": regressions, "paired_interval": interval, "checks": checks,
              "external_quality_pass": all(checks.values()), "primary_replaced": False}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Fresh", {name: {key: value for key, value in result.items() if key != "points"} for name, result in final.items()}, flush=True)
    print("Checks", checks, flush=True)


if __name__ == "__main__":
    main()
