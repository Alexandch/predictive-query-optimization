"""Post-control historical-target drift diagnostic, never a model correction."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def summarize(rows, targets):
    result = {}
    for cohort in ("known_retained", "known_dropped"):
        subset = [row for row in rows if row["cohort"] == cohort and row["status"] == "ok"]
        if not subset:
            raise ValueError("Both known cohorts need successful measurements")
        ratios = [targets[row["sql"]] / max(row["actual_ms"], .001) for row in subset]
        result[cohort] = {
            "queries": len(subset),
            "median_historical_to_fresh_ratio": float(np.median(ratios)),
            "historical_gt_1_5x_fresh": sum(value > 1.5 for value in ratios),
            "historical_lt_fresh_over_1_5": sum(value < 1 / 1.5 for value in ratios),
            "baseline_overpredictions": sum(row["baseline_ms"] > row["actual_ms"] for row in subset),
            "candidate_overpredictions": sum(row["candidate_ms"] > row["actual_ms"] for row in subset),
        }
    return {"diagnostic_only": True, "models_corrected": False, "gate_changed": False,
            "causal_explanation_established": False, "cohorts": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, default=Path("artifacts/paired-regression-20261027"))
    parser.add_argument("--source", type=Path, default=Path("artifacts/structural-sql-v3-20261027/training.csv"))
    args = parser.parse_args()
    manifest = json.loads((args.control / "manifest.json").read_text(encoding="utf-8"))
    if manifest["files"].get(str(args.source)) != hashlib.sha256(args.source.read_bytes()).hexdigest():
        raise ValueError("Historical source does not match frozen control")
    rows = json.loads((args.control / "measurements.json").read_text(encoding="utf-8"))
    source = pd.read_csv(args.source)
    targets = source.groupby("sql_text").actual_total_time_ms.median().to_dict()
    report = summarize(rows, targets)
    (args.control / "historical-drift.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
