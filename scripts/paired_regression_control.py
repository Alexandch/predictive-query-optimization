"""Freeze and run a paired regression control. No training or promotion."""

import argparse
import hashlib
import json
from pathlib import Path
import random
from time import perf_counter

import pandas as pd
from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.database_features import collect_database_features
from pqo.explain import collect_explain, measure_query_execution
from pqo.model_cache import load_joblib_model
from pqo.paired_regression import CRITERIA, assess
from pqo.paired_workload import paired_workload
from pqo.sql_features import extract_sql_features
from pqo.training import predict_query_time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(sql):
    return parse_one(sql, read="postgres").sql(dialect="postgres", comments=False)


def pick_families(frame, count):
    rows = frame.drop_duplicates("sql_text").to_dict("records")
    rows.sort(key=lambda row: hashlib.sha256(("paired-20261027" + row["sql_text"]).encode()).hexdigest())
    chosen = {}
    for row in rows:
        chosen.setdefault(row["template_id"], row)
    families = sorted(chosen, key=lambda family: hashlib.sha256(("families-20261027" + family).encode()).hexdigest())
    if len(families) < count:
        raise ValueError(f"Only {len(families)} families available; protocol requires {count}")
    return [chosen[family] for family in families[:count]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/paired-regression-20261027"))
    parser.add_argument("--run", action="store_true", help="Otherwise freeze manifest only")
    parser.add_argument("--resume", action="store_true", help="Append only missing cases; never remeasure completed/failed cases")
    args = parser.parse_args()
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    candidate = Path("artifacts/plan-flow-experiment-20261027/plan-v1/xgboost_query_time.joblib")
    historical = Path("artifacts/structural-sql-v3-20261027/training.csv")
    fitted = Path("artifacts/plan-flow-experiment-20261027/training.csv")
    source = pd.read_csv(historical)
    retained = pd.read_csv(fitted)
    present = set(retained.sql_text)
    ordinary = source[source.sql_text.isin(present)
                      & (source.join_count <= 2) & (source.subquery_count <= 1)
                      & ~source.sql_text.str.contains(r"\bOVER\s*\(", case=False, regex=True)]
    dropped = source[~source.sql_text.isin(present)]
    cases = []
    for cohort, frame in (("known_retained", ordinary), ("known_dropped", dropped)):
        cases.extend({"cohort": cohort, "family": str(row["template_id"]), "sql": str(row["sql_text"])}
                     for row in pick_families(frame, 20))
    old_sql = {normalized(sql) for sql in set(source.sql_text) | set(retained.sql_text)}
    # Exclude previous held-out queries too; they remain diagnostics, not a new
    # independent control merely because we execute them a second time.
    for name in ("complex-aggregation-development-v2", "broad-first-prediction-20261027-v2",
                 "structural-sql-v3-20261027", "plan-flow-experiment-20261027"):
        manifest = json.loads((Path("artifacts") / name / "manifest.json").read_text(encoding="utf-8"))
        old_sql.update(normalized(item["sql"]) for item in manifest["cases"])
    fresh = list(paired_workload())
    fresh_sql = {normalized(case.sql_text) for case in fresh}
    if len(fresh_sql) != 36 or fresh_sql & old_sql:
        raise ValueError("Fresh control must contain 36 unique previously unused SQL")
    cases.extend({"cohort": "fresh", "family": case.template_id, "sql": case.sql_text} for case in fresh)
    random.Random(20261027).shuffle(cases)
    for index, case in enumerate(cases):
        case["id"] = index
    settings = DatabaseSettings.from_env()
    files = {str(path): digest(path) for path in (baseline, candidate, historical, fitted)}
    protocol = {"files": files, "criteria": CRITERIA, "warmups": 1, "repetitions": 3,
                "statement_timeout_ms": settings.statement_timeout_ms,
                "database": {"host": settings.host, "port": settings.port, "name": settings.dbname},
                "seed": 20261027, "training_allowed": False, "automatic_promotion": False,
                "known_cohorts_are_not_independent": True, "cases": cases}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != protocol:
            raise ValueError("Frozen protocol/model/data changed; do not reuse this control directory")
    else:
        manifest.write_text(json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Frozen: 36 fresh SQL + 20 ordinary retained + 20 historically dropped", flush=True)
    if not args.run:
        return
    results_path = args.output / "measurements.json"
    if results_path.exists() and not args.resume:
        raise FileExistsError("Use --resume to append missing cases without rerunning completed cases")
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
    completed = {row["id"] for row in results}
    if len(completed) != len(results) or not completed <= set(range(len(cases))):
        raise ValueError("Invalid checkpoint case IDs")
    for row in results:
        if any(row.get(key) != cases[row["id"]][key] for key in ("sql", "cohort", "family")):
            raise ValueError("Checkpoint does not match frozen cases")
    load_joblib_model(baseline)
    load_joblib_model(candidate)
    # Complete all first predictions before measuring any remaining SQL.
    predictions = {}
    for case in cases:
        if case["id"] in completed:
            continue
        plan = collect_explain(case["sql"], settings=settings, analyze=False)
        features = {**extract_sql_features(case["sql"]).as_dict(), **plan.features.as_dict(),
                    **collect_database_features(case["sql"], plan.features.estimated_plan_rows, settings=settings).as_dict()}
        values = {}
        for label, model in (("baseline", baseline), ("candidate", candidate)):
            start = perf_counter()
            values[label + "_ms"] = predict_query_time(model, features)
            values[label + "_inference_ms"] = (perf_counter() - start) * 1000
        predictions[case["id"]] = values
    prediction_path = args.output / ("remaining-predictions.json" if completed else "predictions.json")
    prediction_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    for case in cases:
        if case["id"] in completed:
            continue
        row = {**case, **predictions[case["id"]]}
        try:
            measurement = measure_query_execution(case["sql"], settings=settings, warmup_runs=1, repetitions=3)
            row.update({"status": "ok", "actual_ms": measurement.median_time_ms,
                        "samples_ms": list(measurement.samples_ms)})
        except Exception as exc:
            row.update({"status": "error", "error_type": type(exc).__name__})
        results.append(row)
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{len(results)}/{len(cases)} {case['cohort']} {case['family']}: {row.get('actual_ms', row.get('error_type'))}", flush=True)
    if any(digest(Path(path)) != value for path, value in files.items()):
        raise ValueError("Frozen model/data files changed during control")
    report = {"baseline_sha256": files[str(baseline)], "candidate_sha256": files[str(candidate)],
              "criteria": CRITERIA, **assess(results, len(cases))}
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
