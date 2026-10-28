"""Post-selection diagnostic on user SQL; never used to fit or select models."""

import argparse
import json
from pathlib import Path

import pandas as pd
from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.fresh_measurements import collect_record, records_frame
from pqo.history import load_analysis_history
from complex_aggregation_experiment import aggregate, evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--training", type=Path)
    args = parser.parse_args()
    selection = json.loads((args.experiment / "selection.json").read_text(encoding="utf-8"))
    selected = args.experiment / selection["selected"] / "xgboost_query_time.joblib"
    previous = Path("artifacts/heavy-fresh-training-20261027-v3/identity/xgboost_query_time.joblib")
    control = {291: "customer_categories", 292: "flight_prices", 293: "redundant_sort"}
    history = {row.query_run_id: row for row in load_analysis_history(limit=200)}
    if not set(control) <= history.keys():
        raise ValueError("Expected local user history IDs are absent; no substitute SQL is used")
    normalize = lambda text: parse_one(text, read="postgres").sql(dialect="postgres", comments=False)
    training = {normalize(text) for text in pd.read_csv(args.training or args.experiment / "training.csv").sql_text.unique()}
    if any(normalize(history[key].sql_text) in training for key in control):
        raise ValueError("User control leaked into fit")
    records = [collect_record({"id": key, "split": "user_control", "family": family,
                               "sql": history[key].sql_text}, DatabaseSettings.from_env())
               for key, family in control.items()]
    (args.experiment / "user-control-measurements.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")
    frame = aggregate(records_frame(records).drop(columns="split"))
    results = {"previous": evaluate(previous, frame), "candidate": evaluate(selected, frame),
               "role": "previously inspected diagnostic only; not fit, selection or fresh acceptance"}
    (args.experiment / "user-control-report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
