"""Collect fresh complex queries and compare first predictions, without calibration."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sqlglot import parse_one

from pqo.complex_workload import complex_workload
from pqo.dataset import collect_sample
from pqo.experiments import assess_candidate
from pqo.explain import collect_explain
from pqo.model_cache import load_joblib_model
from pqo.training import TARGET_COLUMN, prepare_prediction_frame, train_xgboost


def identity(sql):
    return parse_one(sql, read="postgres").sql(dialect="postgres")


def aggregate(frame):
    return frame.groupby(["template_id", "sql_text"], as_index=False).agg({
        name: "median" if name == TARGET_COLUMN else "first"
        for name in frame.columns if name not in ("template_id", "sql_text")
    })


def evaluate(path, frame):
    artifact = load_joblib_model(path)
    prediction = artifact["pipeline"].predict(prepare_prediction_frame(frame, artifact))
    if artifact["target_transform"] == "log1p":
        prediction = np.expm1(prediction)
    prediction = np.maximum(prediction, 0.001)
    actual = np.maximum(frame[TARGET_COLUMN].to_numpy(dtype=float), 0.001)
    return {
        "queries": len(frame),
        "mae_ms": float(np.mean(np.abs(prediction - actual))),
        "median_q_error": float(np.median(np.maximum(actual, prediction) / np.minimum(actual, prediction))),
        "within_20_percent": float(np.mean(np.abs(prediction - actual) <= actual * .2)),
        "points": [{"template": row.template_id, "actual_ms": float(actual[i]), "predicted_ms": float(prediction[i])}
                   for i, row in enumerate(frame.itertuples())],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/complex-aggregation-development"))
    parser.add_argument("--reuse-measurements", action="store_true")
    parser.add_argument("--base", type=Path, default=Path("artifacts/window-cte-development/training.csv"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    baseline_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()
    cases = list(complex_workload())
    manifest = [{"split": split, "family": case.template_id, "sql": case.sql_text}
                for split, case in cases]
    manifest_path = args.output / "manifest.json"
    protocol = {
        "baseline_sha256": baseline_hash,
        "base_dataset_sha256": hashlib.sha256(args.base.read_bytes()).hexdigest(),
        "candidates": ["identity", "log1p"], "feature_version": "sql-v2",
        "selection": "validation MAE only; acceptance once after selection",
        "repetitions": 3, "warmups": 1,
        "promotion_requires": "validation and acceptance MAE/Q-error improve; old-data MAE <=110%; existing internal gate",
        "cases": manifest,
    }
    if not args.reuse_measurements:
        if manifest_path.exists():
            raise FileExistsError("Use a new output directory or --reuse-measurements")
        manifest_path.write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    else:
        if json.loads(manifest_path.read_text(encoding="utf-8")) != protocol:
            raise ValueError("Protocol or baseline changed since collection")

    base = pd.read_csv(args.base)
    sets = {split: {identity(case.sql_text) for label, case in cases if label == split}
            for split in ("train", "validation", "acceptance")}
    assert not (sets["train"] & sets["validation"] or sets["train"] & sets["acceptance"] or sets["validation"] & sets["acceptance"])
    existing = {identity(sql) for sql in base.sql_text.unique()}
    assert not existing.intersection(set.union(*sets.values())), "Fresh queries overlap old training data"
    # A previous experiment's held-out queries are never appended to training.
    old_manifest = json.loads(Path("artifacts/window-cte-development/split-manifest.json").read_text(encoding="utf-8"))
    prior_holdout = {identity(item["sql"]) for item in old_manifest if item["split"] != "train"}
    assert not prior_holdout.intersection(existing | sets["train"])

    measured = args.output / "measurements.csv"
    if not args.reuse_measurements:
        rows = []
        for _, case in cases:
            collect_explain(case.sql_text, analyze=False)
        print("All 60 queries passed PostgreSQL EXPLAIN preflight", flush=True)
        for number, (split, case) in enumerate(cases, 1):
            collect_sample(case, persist=False)
            samples = [collect_sample(case, persist=False).values for _ in range(3)]
            rows.extend({**sample, "split": split} for sample in samples)
            pd.DataFrame(rows).to_csv(measured, index=False)
            print(f"{number}/{len(cases)} {case.template_id}: {np.median([sample[TARGET_COLUMN] for sample in samples]):.1f} ms", flush=True)

    fresh = pd.read_csv(measured)
    if len(fresh) != len(cases) * 3:
        raise ValueError("Incomplete measurements; do not fit on a partial workload")
    train = fresh[fresh.split == "train"].drop(columns="split")
    validation = aggregate(fresh[fresh.split == "validation"].drop(columns="split"))
    acceptance = aggregate(fresh[fresh.split == "acceptance"].drop(columns="split"))
    combined = args.output / "training.csv"
    pd.concat([base, train], ignore_index=True).to_csv(combined, index=False)
    candidates = {}
    for transform in protocol["candidates"]:
        destination = args.output / transform
        train_xgboost(combined, destination, tune=False, feature_version="sql-v2",
                      target_transform=transform, template_balanced=True)
        candidates[transform] = destination / "xgboost_query_time.joblib"
    validation_results = {name: evaluate(path, validation) for name, path in candidates.items()}
    selected = min(candidates, key=lambda name: validation_results[name]["mae_ms"])
    old_validation = evaluate(baseline, validation)
    old_acceptance = evaluate(baseline, acceptance)
    final_acceptance = evaluate(candidates[selected], acceptance)
    regression = aggregate(base).sample(n=500, random_state=1023)
    old_regression = evaluate(baseline, regression)
    new_regression = evaluate(candidates[selected], regression)
    selected_metrics = json.loads(candidates[selected].with_name("metrics.json").read_text(encoding="utf-8"))
    current_metrics = json.loads(baseline.with_name("metrics.json").read_text(encoding="utf-8"))
    internal = assess_candidate("xgboost", selected_metrics, current_metrics)
    gate = (
        internal.allowed
        and validation_results[selected]["mae_ms"] < old_validation["mae_ms"]
        and validation_results[selected]["median_q_error"] < old_validation["median_q_error"]
        and final_acceptance["mae_ms"] < old_acceptance["mae_ms"]
        and final_acceptance["median_q_error"] < old_acceptance["median_q_error"]
        and new_regression["mae_ms"] <= old_regression["mae_ms"] * 1.10
    )
    report = {"baseline_sha256": baseline_hash, "training_queries_added": train.sql_text.nunique(),
              "selected": selected, "validation": {"baseline": old_validation, **validation_results},
              "acceptance": {"baseline": old_acceptance, "selected": final_acceptance},
              "known_data_regression_not_independent": {"baseline": old_regression, "selected": new_regression},
              "internal_gate": {"allowed": internal.allowed, "explanation": internal.explanation},
              "promotion_allowed": gate, "primary_replaced": False}
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == baseline_hash
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for section in ("validation", "acceptance", "known_data_regression_not_independent"):
        print(section, {key: {k: v for k, v in value.items() if k != "points"} for key, value in report[section].items()}, flush=True)
    print("selected", selected, "promotion_allowed", gate, "internal_gate", internal.allowed, flush=True)


if __name__ == "__main__":
    main()
