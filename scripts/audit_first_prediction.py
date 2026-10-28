"""Describe existing predictions by family; never fit or reselect a model."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median


def summarize(points):
    if not points:
        raise ValueError("At least one prediction is required")
    families = defaultdict(list)
    for point in points:
        actual = float(point["actual_ms"])
        predicted = float(point["predicted_ms"])
        if not all(math.isfinite(value) and value > 0 for value in (actual, predicted)):
            raise ValueError("Execution times must be positive and finite")
        families[point["template"]].append({
            **point,
            "absolute_error_ms": abs(predicted - actual),
            "q_error": max(actual, predicted) / min(actual, predicted),
            "direction": "over" if predicted > actual else "under" if predicted < actual else "equal",
        })
    rows = [row for group in families.values() for row in group]
    return {
        "families": {name: {
            "queries": len(group),
            "mae_ms": mean(row["absolute_error_ms"] for row in group),
            "median_q_error": median(row["q_error"] for row in group),
            "over_factor_two": sum(row["q_error"] > 2 for row in group),
        } for name, group in sorted(families.items())},
        "macro_family_mae_ms": mean(mean(row["absolute_error_ms"] for row in group)
                                    for group in families.values()),
        "over_factor_two": sum(row["q_error"] > 2 for row in rows),
        "worst_absolute_errors": sorted(rows, key=lambda row: row["absolute_error_ms"], reverse=True)[:5],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = {"source": str(args.report), "diagnostic_only": True,
              "selected": report["selected"],
              "splits": {split: {name: summarize(value["points"])
                                 for name, value in report[split].items()}
                         for split in ("validation", "acceptance")}}
    destination = args.report.with_name("error-audit.json")
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    for split, models in result["splits"].items():
        for name, summary in models.items():
            print(split, name, "family MAE", round(summary["macro_family_mae_ms"], 2),
                  "errors >2x", summary["over_factor_two"], flush=True)


if __name__ == "__main__":
    main()
