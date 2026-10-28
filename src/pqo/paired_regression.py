"""Paired metrics on frozen SQL and common targets, with no model fitting."""

import numpy as np


CRITERIA = {
    "fresh_mae_ratio_max": .90, "fresh_q_ratio_max": 1.0,
    "regression_mae_ratio_max": 1.10, "regression_q_ratio_max": 1.05,
    "regression_p90_ratio_max": 1.10, "regression_20pp_loss_max": .05,
    "bootstrap_seed": 20261027, "bootstrap_repetitions": 10000,
    "bootstrap_upper_must_be_negative": True,
}


def metrics(rows, model):
    actual = np.maximum([row["actual_ms"] for row in rows], .001)
    predicted = np.maximum([row[model + "_ms"] for row in rows], .001)
    if not len(rows) or not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise ValueError("Finite, nonempty measurements are required")
    errors = np.abs(actual - predicted)
    return {"queries": len(rows), "families": len({row["family"] for row in rows}),
            "mae_ms": float(errors.mean()), "p90_absolute_error_ms": float(np.quantile(errors, .9)),
            "median_q_error": float(np.median(np.maximum(actual, predicted) / np.minimum(actual, predicted))),
            "within_20_percent": float(np.mean(errors <= actual * .2))}


def cluster_interval(rows):
    # Paired differences are averaged within a family before bootstrap. Scale
    # variants are not falsely treated as independent query structures.
    groups = {}
    for row in rows:
        difference = abs(row["candidate_ms"] - row["actual_ms"]) - abs(row["baseline_ms"] - row["actual_ms"])
        groups.setdefault(row["family"], []).append(difference)
    values = np.array([np.mean(group) for _, group in sorted(groups.items())])
    if len(values) < 8:
        raise ValueError("At least eight fresh families are required")
    rng = np.random.default_rng(CRITERIA["bootstrap_seed"])
    draws = rng.choice(values, size=(CRITERIA["bootstrap_repetitions"], len(values)), replace=True).mean(axis=1)
    lower, upper = np.quantile(draws, [.025, .975])
    return {"families": len(values), "mean_paired_family_error_difference_ms": float(values.mean()),
            "lower_95_ms": float(lower), "upper_95_ms": float(upper)}


def assess(rows, expected_queries):
    complete = len(rows) == expected_queries and all(row.get("status") == "ok" for row in rows)
    result = {"complete": complete, "paired_quality_pass": False,
              "automatic_promotion": False, "previous_internal_gate_overridden": False}
    if not complete:
        result["reason"] = "Incomplete control or failed/timed-out SQL; no failed cases are dropped"
        return result
    cohorts = {}
    checks = {}
    for cohort in ("fresh", "known_retained", "known_dropped"):
        subset = [row for row in rows if row["cohort"] == cohort]
        old, new = metrics(subset, "baseline"), metrics(subset, "candidate")
        cohorts[cohort] = {"baseline": old, "candidate": new}
        if cohort == "fresh":
            checks[cohort] = (new["mae_ms"] <= old["mae_ms"] * CRITERIA["fresh_mae_ratio_max"]
                             and new["median_q_error"] <= old["median_q_error"] * CRITERIA["fresh_q_ratio_max"]
                             and new["within_20_percent"] >= old["within_20_percent"])
        else:
            checks[cohort] = (
                new["mae_ms"] <= old["mae_ms"] * CRITERIA["regression_mae_ratio_max"]
                and new["median_q_error"] <= old["median_q_error"] * CRITERIA["regression_q_ratio_max"]
                and new["p90_absolute_error_ms"] <= old["p90_absolute_error_ms"] * CRITERIA["regression_p90_ratio_max"]
                and new["within_20_percent"] >= old["within_20_percent"] - CRITERIA["regression_20pp_loss_max"])
    interval = cluster_interval([row for row in rows if row["cohort"] == "fresh"])
    checks["fresh_paired_cluster_interval"] = interval["upper_95_ms"] < 0
    result.update({"cohorts": cohorts, "checks": checks, "fresh_paired_interval": interval,
                   "paired_quality_pass": all(checks.values())})
    return result
