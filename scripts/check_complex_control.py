"""One diagnostic evaluation after candidate selection; never selects a model."""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from sqlglot import parse_one
from pqo.explain import measure_query_execution
from pqo.prediction import predict_sql_query


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.experiment / "report.json").read_text(encoding="utf-8"))
    destination = args.experiment / "user-control-diagnostic.json"
    if destination.exists():
        raise FileExistsError("Diagnostic already recorded; do not repeatedly tune on this control")
    sql = Path("examples/retail-category-share-control.sql").read_text(encoding="utf-8")
    def canonical(text):
        return parse_one(text, read="postgres").sql(dialect="postgres", comments=False)
    training = pd.read_csv(args.experiment / "training.csv")
    assert canonical(sql) not in {canonical(text) for text in training.sql_text.unique()}
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == report["baseline_sha256"]
    candidate = args.experiment / report["selected"] / "xgboost_query_time.joblib"
    predictions = {
        name: predict_sql_query(sql, path, calibration_profile_path=None).predicted_time_ms
        for name, path in (("baseline", baseline), ("selected", candidate))
    }
    measured = measure_query_execution(sql, warmup_runs=1, repetitions=5)
    result = {
        "note": "Diagnostic only; query excluded from training and selection; no calibration",
        "model_selection_report_sha256": hashlib.sha256((args.experiment / "report.json").read_bytes()).hexdigest(),
        "predictions_ms": predictions, "measured_median_ms": measured.median_time_ms,
        "samples_ms": measured.samples_ms,
        "relative_errors_percent": {name: abs(value - measured.median_time_ms) / measured.median_time_ms * 100
                                    for name, value in predictions.items()},
    }
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
