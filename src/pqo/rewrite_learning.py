"""Offline rewrite-efficiency research. Never executes SQL or activates models."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from statistics import median

from .plan_features import extract_plan_features, _unwrap_explain
from .rewrite_journal import iter_journal, sql_identity

FEATURE_VERSION = 1
MIN_DEVELOPMENT_FAMILIES = 30
MIN_CONTROL_FAMILIES = 10


def rewrite_features(original_sql, variant_sql, rule_id, estimated_plans):
    """Allowlisted, pre-execution features only; never actual times or buffers."""
    from sqlglot import exp, parse_one
    result = {"rule_id": str(rule_id)}
    kinds = (exp.Select, exp.Join, exp.Subquery, exp.CTE, exp.Window,
             exp.AggFunc, exp.Distinct, exp.Order, exp.Group, exp.Having,
             exp.Where, exp.Limit, exp.Or, exp.Exists)
    for side, sql in (("baseline", original_sql), ("variant", variant_sql)):
        tree = parse_one(sql, read="postgres")
        counts = Counter(type(node).__name__ for node in tree.walk())
        result[f"{side}.ast_nodes"] = sum(counts.values())
        for kind in kinds:
            result[f"{side}.sql.{kind.__name__}"] = sum(isinstance(node, kind) for node in tree.walk())
        # Never run this extractor on measured plans. Even if a foreign record
        # includes actual fields, only these estimates/structural counts survive.
        root = _unwrap_explain(estimated_plans[side])
        if not all(key in root for key in ("Node Type", "Total Cost", "Plan Rows")):
            raise ValueError("Incomplete estimated plan")
        plan = extract_plan_features(estimated_plans[side]).as_dict()
        for name, value in plan.items():
            if name.startswith("estimated_") or name in {
                "node_count", "max_plan_depth", "relation_count", "root_node_type",
                "seq_scan_count", "index_scan_count", "index_only_scan_count",
                "bitmap_heap_scan_count", "hash_join_count", "merge_join_count",
                "nested_loop_count", "sort_node_count", "aggregate_node_count",
            }:
                if isinstance(value, str):
                    result[f"{side}.plan.{name}"] = value
                elif isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    result[f"{side}.plan.{name}"] = math.log1p(value)
                else:
                    raise ValueError("Invalid estimated plan feature")
    return result


def _validated_label(run, candidate):
    """Do not trust an eligibility flag in an imported or edited journal."""
    if run.get("status") != "completed" or run.get("error"):
        raise ValueError("incomplete_run")
    if candidate.get("status") != "completed" or candidate.get("error"):
        raise ValueError("incomplete_candidate")
    if candidate.get("equivalence") is not True:
        raise ValueError("unproven_equivalence")
    protocol = run.get("protocol") or {}
    reps = protocol.get("repetitions")
    if (type(reps) is not int or reps < 3 or protocol.get("order") != "alternating"
            or protocol.get("pair_warmups") != 1):
        raise ValueError("insufficient_protocol")
    medians = []
    measurements = candidate.get("measurements") or []
    for side in ("baseline", "variant"):
        warmups = [item.get("time_ms") for item in measurements if isinstance(item, dict)
                   and item.get("side") == side and item.get("phase") == "warmup"]
        if len(warmups) != 1 or not all(type(value) in (float, int) and math.isfinite(value)
                                        and value > 0 for value in warmups):
            raise ValueError("missing_warmup")
    order = [item.get("side") for item in measurements if isinstance(item, dict)
             and item.get("phase") == "measurement"]
    expected = [side for index in range(reps) for side in
                (("baseline", "variant") if index % 2 == 0 else ("variant", "baseline"))]
    if order != expected:
        raise ValueError("pair_order_mismatch")
    for side, field in (("baseline", "baseline_time_ms"), ("variant", "rewritten_time_ms")):
        samples = candidate.get(f"{side}_samples_ms")
        if not isinstance(samples, list) or len(samples) != reps or not all(
            type(value) in (float, int) and math.isfinite(value) and value > 0 for value in samples
        ):
            raise ValueError("invalid_samples")
        raw = [item.get("time_ms") for item in measurements
               if isinstance(item, dict) and item.get("side") == side and item.get("phase") == "measurement"]
        if raw != samples:
            raise ValueError("sample_record_mismatch")
        value = candidate.get(field)
        if type(value) not in (float, int) or not math.isfinite(value) or not math.isclose(value, median(samples), rel_tol=1e-6):
            raise ValueError("median_mismatch")
        medians.append(value)
    return math.log(medians[0]) - math.log(medians[1])


def prepare_dataset(directories, manifest=None):
    """Explicit run selection; linked SQL shapes cannot cross the holdout boundary."""
    from sqlglot.errors import SqlglotError
    manifest = {} if manifest is None else manifest
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    selected = {}
    for role in ("development", "control"):
        ids = manifest.get(f"{role}_run_ids", [])
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            raise ValueError("Manifest run IDs must be lists of strings")
        for run_id in ids:
            if run_id in selected:
                raise ValueError("Duplicate or overlapping run IDs in manifest")
            selected[run_id] = role
    report = {"feature_version": FEATURE_VERSION, "runs": 0, "technical_candidates": 0,
              "excluded": Counter(), "rows": {"development": 0, "control": 0},
              "families": {"development": 0, "control": 0}, "unassigned_run_ids": [],
              "training_ready": False}
    seen, rows, found = {}, [], set()
    for directory in directories:
        for run in iter_journal(directory):
            run_id = run["run_id"]
            digest = hashlib.sha256(json.dumps(run, sort_keys=True).encode()).hexdigest()
            if run_id in seen:
                if seen[run_id] != digest:
                    raise ValueError("Conflicting copies of a journal run")
                continue
            seen[run_id] = digest
            report["runs"] += 1
            if run_id in selected:
                found.add(run_id)
            if (run.get("source") == "smoke_test" or run.get("dataset_role") == "test"
                    or (run.get("dataset_role") == "control" and selected.get(run_id) != "control")):
                report["excluded"]["protected_test_or_control"] += 1
                continue
            if run.get("status") == "unreadable":
                report["excluded"]["unreadable_run"] += 1
                continue
            if not run.get("candidates"):
                report["excluded"]["no_candidates"] += 1
            eligible_in_run = False
            for candidate in run.get("candidates", []):
                try:
                    label = _validated_label(run, candidate)
                    features = rewrite_features(run["original_sql"], candidate["variant_sql"],
                                                candidate["rule_id"], candidate["estimated_plans"])
                    original = sql_identity(run["original_sql"])
                    variant = sql_identity(candidate["variant_sql"])
                    fingerprint = run["database_fingerprint"]
                    if not isinstance(fingerprint, str) or not fingerprint:
                        raise ValueError("missing_database_identity")
                    collection = run.get("collection") or {}
                    if not isinstance(collection, dict):
                        raise ValueError("malformed_candidate")
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError, SqlglotError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                        "incomplete_run", "incomplete_candidate", "unproven_equivalence", "insufficient_protocol",
                        "invalid_samples", "sample_record_mismatch", "median_mismatch", "missing_database_identity",
                        "missing_warmup", "pair_order_mismatch",
                        "Invalid estimated plan feature", "Incomplete estimated plan",
                    } else "malformed_candidate"
                    report["excluded"][reason] += 1
                    continue
                report["technical_candidates"] += 1
                eligible_in_run = True
                if run_id not in selected:
                    report["excluded"]["unassigned_candidate"] += 1
                rows.append({"run_id": run_id, "role": selected.get(run_id), "features": features,
                             "label": label, "database_fingerprint": fingerprint,
                             "original_hash": original["sql_hash"], "variant_hash": variant["sql_hash"],
                             "shapes": (original["shape_hash"], variant["shape_hash"]),
                             "collection_family": collection.get("family_id")})
            if eligible_in_run and run_id not in selected:
                report["unassigned_run_ids"].append(run_id)
    if set(selected) - found:
        raise ValueError("Manifest references unknown run IDs")

    # Connected components include original AND variant, across databases too.
    # Thus a rewrite of A into B cannot leak if B later appears as an original.
    parents = {}
    def root(value):
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value
    for row in rows:
        first, second = map(root, row["shapes"])
        parents[second] = first
        if row["collection_family"]:
            family = root("collection:" + str(row["collection_family"]))
            parents[family] = root(first)
    family_roles = {}
    for row in rows:
        family = root(row["shapes"][0])
        if row["role"] is None:
            continue
        if family in family_roles and family_roles[family] != row["role"]:
            raise ValueError("SQL family leakage between development and control")
        family_roles[family] = row["role"]
        row["family"] = family

    # Repeated clicks must not inflate dataset size or sample weight. Collapse
    # identical feature contexts; different estimated plans remain distinct.
    grouped = {}
    for row in rows:
        if row["role"] is None:
            continue
        identity = (row["role"], row["database_fingerprint"], row["original_hash"],
                    row["variant_hash"], json.dumps(row["features"], sort_keys=True))
        grouped.setdefault(identity, []).append(row)
    result = []
    for group in grouped.values():
        row = dict(group[0], label=median(item["label"] for item in group),
                   run_ids=sorted(item["run_id"] for item in group))
        result.append(row)
    for role in report["rows"]:
        report["rows"][role] = sum(row["role"] == role for row in result)
        report["families"][role] = len({row["family"] for row in result if row["role"] == role})
    report["excluded"] = dict(report["excluded"])
    report["training_ready"] = (report["families"]["development"] >= MIN_DEVELOPMENT_FAMILIES
                                and report["families"]["control"] >= MIN_CONTROL_FAMILIES)
    report["dataset_sha256"] = hashlib.sha256(json.dumps(
        sorted(result, key=lambda row: (row["role"], row["run_ids"])), sort_keys=True,
        allow_nan=False).encode("utf-8")).hexdigest()
    return result, report


def train_experiment(rows, report):
    """Candidate-only regressor for log(baseline/variant); no application mutation."""
    development_families = {row["family"] for row in rows if row["role"] == "development"}
    control_families = {row["family"] for row in rows if row["role"] == "control"}
    if development_families & control_families:
        raise ValueError("SQL family leakage between development and control")
    if (not report["training_ready"] or len(development_families) < MIN_DEVELOPMENT_FAMILIES
            or len(control_families) < MIN_CONTROL_FAMILIES):
        raise ValueError("Not enough independent families: need 30 development and 10 control")
    from sklearn.feature_extraction import DictVectorizer
    from xgboost import XGBRegressor
    import numpy as np
    train = [row for row in rows if row["role"] == "development"]
    control = [row for row in rows if row["role"] == "control"]
    vectorizer = DictVectorizer(sparse=False)
    x_train = vectorizer.fit_transform([row["features"] for row in train])
    x_control = vectorizer.transform([row["features"] for row in control])
    y_train = np.array([row["label"] for row in train])
    y_control = np.array([row["label"] for row in control])
    # Every independent family has total weight one, including control metrics.
    train_sizes = Counter(row["family"] for row in train)
    control_sizes = Counter(row["family"] for row in control)
    weights = np.array([1 / control_sizes[row["family"]] for row in control])
    model = XGBRegressor(n_estimators=120, max_depth=3, learning_rate=0.04,
                         objective="reg:squarederror", n_jobs=1, random_state=42)
    model.fit(x_train, y_train, sample_weight=[1 / train_sizes[row["family"]] for row in train])
    predictions = model.predict(x_control)
    def mae(values):
        return float(np.average(np.abs(values - y_control), weights=weights))
    fixed = median(row["label"] for row in train)
    # Rule baseline also uses development only. No control-based tuning.
    rules = {}
    for rule in {row["features"]["rule_id"] for row in train}:
        rules[rule] = median(row["label"] for row in train if row["features"]["rule_id"] == rule)
    rule_prediction = np.array([rules.get(row["features"]["rule_id"], fixed) for row in control])
    constant_mae, rule_mae, model_mae = mae(fixed), mae(rule_prediction), mae(predictions)
    metrics = {"target": "log_baseline_over_variant", "constant_mae": constant_mae,
               "rule_mae": rule_mae, "model_mae": model_mae,
               "beats_baselines": bool(model_mae < 0.95 * min(constant_mae, rule_mae)),
               "automatic_activation": False,
               "note": "Offline pointwise efficiency estimate; not proof of query equivalence or a ranking evaluation"}
    return {"feature_version": FEATURE_VERSION, "vectorizer": vectorizer, "model": model,
            "metrics": metrics, "dataset_report": report,
            "provenance": [{key: row[key] for key in ("run_ids", "role", "family")} for row in rows]}, metrics


def main():
    from .app_paths import writable_root
    parser = argparse.ArgumentParser(description="Audit and train offline rewrite-efficiency candidates; never activates a model")
    parser.add_argument("--directory", type=Path, action="append")
    parser.add_argument("--manifest", type=Path, help="Explicit development_run_ids and control_run_ids")
    parser.add_argument("--report", type=Path, required=True, help="New report path (not overwritten)")
    parser.add_argument("--train-output", type=Path, help="New offline experiment directory; needs sufficient families")
    args = parser.parse_args()
    if args.report.exists() or (args.train_output and args.train_output.exists()):
        parser.error("Output already exists; choose a new path")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    rows, report = prepare_dataset(args.directory or [writable_root() / "artifacts/rewrite-journal"], manifest)
    if args.train_output:
        if not report["training_ready"]:
            report["training_refused"] = "Need 30 development and 10 control families; no model produced"
        else:
            artifact, metrics = train_experiment(rows, report)
            import joblib
            args.train_output.mkdir(parents=True, exist_ok=False)
            joblib.dump(artifact, args.train_output / "candidate.joblib")
            report["experiment_metrics"] = metrics
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if "training_refused" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
