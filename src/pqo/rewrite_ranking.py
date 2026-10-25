"""Frozen-model, multi-candidate offline replay. Never activates or retrains a model."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path

from .config import DatabaseSettings
from .rewrite_batch import BatchCase, _identifier, _save, collect_batch
from .rewrite_journal import iter_journal, sql_identity, database_fingerprint
from .rewrite_learning import FEATURE_VERSION, prepare_dataset, rewrite_features, _validated_label
from .sql_rewrite import generate_sql_rewrites


def ranking_cases(relationships):
    """New query shapes, but ONLY previously held-out relationship families."""
    result = []
    for relation in sorted(set(relationships), key=lambda item: tuple(asdict(item).values())):
        child = f"{_identifier(relation.child_schema)}.{_identifier(relation.child_table)}"
        parent = f"{_identifier(relation.parent_schema)}.{_identifier(relation.parent_table)}"
        ck, pk = _identifier(relation.child_key), _identifier(relation.parent_key)
        templates = {
            "rank-scalar-range": f"SELECT p.{pk}, (SELECT COUNT(*) FROM {child} c WHERE c.{ck}=p.{pk}) AS n FROM {parent} p WHERE p.{pk}>=1 AND p.{pk}<=8 ORDER BY p.{pk}",
            "rank-distinct-or-range": f"SELECT DISTINCT p.{pk} FROM {parent} p JOIN {child} c ON c.{ck}=p.{pk} WHERE (p.{pk}=1 OR p.{pk}=2 OR p.{pk}=3) AND (c.{ck}>=1 AND c.{ck}<=256) ORDER BY p.{pk}",
            "rank-group-sort-having": f"SELECT q.{ck}, COUNT(*) AS n FROM (SELECT c.{ck} FROM {child} c WHERE c.{ck}>=1 AND c.{ck}<=256 ORDER BY c.{ck} DESC) q GROUP BY q.{ck} HAVING q.{ck}>1 ORDER BY q.{ck}",
        }
        for template, sql in templates.items():
            candidates = generate_sql_rewrites(sql)
            if len(candidates) < 2:
                raise ValueError(f"Ranking fixture has fewer than two candidates: {template}")
            case_id = hashlib.sha256((relation.family_id + template + sql).encode()).hexdigest()
            result.append(BatchCase(case_id, relation.family_id, "control", template, sql))
    return result


def frozen_holdout(artifact, journal):
    """Rebuild provenance/digest and reject any training relationship overlap."""
    if artifact.get("feature_version") != FEATURE_VERSION:
        raise ValueError("Unsupported feature version")
    manifest = {f"{role}_run_ids": sorted({run_id for row in artifact["provenance"] if row["role"] == role
                                           for run_id in row["run_ids"]}) for role in ("development", "control")}
    rows, report = prepare_dataset([journal], manifest)
    if report["dataset_sha256"] != artifact["dataset_report"]["dataset_sha256"]:
        raise ValueError("Training journal does not match the frozen model dataset")
    run_roles = {run_id: row["role"] for row in rows for run_id in row["run_ids"]}
    families = {"development": set(), "control": set()}
    training_shapes = set()
    for run in iter_journal(journal):
        role = run_roles.get(run["run_id"])
        if role is None:
            continue
        family = (run.get("collection") or {}).get("family_id")
        if not isinstance(family, str) or not family:
            raise ValueError("Missing relationship provenance")
        families[role].add(family)
        if role == "development":
            training_shapes.add(sql_identity(run["original_sql"])["shape_hash"])
            training_shapes.update(sql_identity(c["variant_sql"])["shape_hash"] for c in run["candidates"])
    if families["development"] & families["control"]:
        raise ValueError("Training/control relationship leakage")
    return families, training_shapes


def _pick(scores, threshold):
    # None is a genuine no-change choice, not a fabricated zero runtime.
    best = max(range(len(scores)), key=lambda index: scores[index])
    return best if scores[best] > threshold else None


def summarize_queries(queries):
    """One validation slot per policy; rejected variant means KEEP ORIGINAL."""
    import numpy as np
    policies = ("model", "generator_first", "estimated_cost", "noop")
    if not queries:
        return {"queries": 0, "families": 0, "passes_ranking_gate": False}
    family_sizes = Counter(row["family_id"] for row in queries)
    weights = np.array([1 / family_sizes[row["family_id"]] for row in queries])
    metrics = {"queries": len(queries), "families": len(family_sizes), "policies": {},
               "minimum_absolute_gain_ms": 2, "minimum_gain_ratio": .05,
               "automatic_activation": False, "passes_ranking_gate": False}
    for policy in policies:
        regrets, realized, matches, useful = [], [], [], []
        for row in queries:
            gains = [candidate["label"] if candidate["accepted"] else 0. for candidate in row["candidates"]]
            oracle = max([0., *gains])
            choice = row["choices"][policy]
            gain = gains[choice] if choice is not None else 0.
            regrets.append(max(0., oracle - gain))
            realized.append(gain)
            matches.append(math.isclose(gain, oracle, abs_tol=1e-9))
            useful.append(gain > 0)
        metrics["policies"][policy] = {
            "mean_log_regret": float(np.average(regrets, weights=weights)),
            "mean_realized_log_gain": float(np.average(realized, weights=weights)),
            "oracle_match_rate": float(np.average(matches, weights=weights)),
            "useful_selections": int(sum(useful)),
            "queries_with_noop_choice": sum(row["choices"][policy] is None for row in queries),
        }
    deltas = []
    for family in sorted(family_sizes):
        members = [row for row in queries if row["family_id"] == family]
        differences = []
        for baseline in policies[1:]:
            values = []
            for row in members:
                def gain(policy):
                    index = row["choices"][policy]
                    return row["candidates"][index]["label"] if index is not None and row["candidates"][index]["accepted"] else 0.
                # Regret(model)-regret(baseline) == gain(baseline)-gain(model).
                values.append(gain(baseline) - gain("model"))
            differences.append(float(np.mean(values)))
        deltas.append(differences)
    deltas = np.array(deltas)
    indices = np.random.default_rng(42).integers(0, len(deltas), size=(1000, len(deltas)))
    ci = np.quantile(deltas[indices].mean(axis=1), [.025, .975], axis=0)
    metrics["regret_difference_ci95"] = {
        baseline: [float(ci[0, index]), float(ci[1, index])]
        for index, baseline in enumerate(policies[1:])}
    model_regret = metrics["policies"]["model"]["mean_log_regret"]
    metrics["passes_ranking_gate"] = bool(len(family_sizes) >= 10 and np.all(ci[1] < 0)
        and all(model_regret < .95 * metrics["policies"][policy]["mean_log_regret"] for policy in policies[1:]))
    return metrics


def replay_ranking(artifact, journal, heldout_families=None):
    from sqlglot.errors import SqlglotError
    if artifact.get("feature_version") != FEATURE_VERSION:
        raise ValueError("Unsupported feature version")
    queries, excluded = [], Counter()
    for run in iter_journal(journal):
        if run.get("dataset_role") != "control" or run.get("source") == "smoke_test":
            excluded["not_control_data"] += 1
            continue
        collection = run.get("collection") or {}
        if not isinstance(collection, dict):
            excluded["invalid_provenance"] += 1
            continue
        if heldout_families is not None and collection.get("family_id") not in heldout_families:
            excluded["not_frozen_holdout_family"] += 1
            continue
        candidates = run.get("candidates", [])
        if len(candidates) < 2:
            excluded["fewer_than_two_candidates"] += 1
            continue
        entries, features, costs = [], [], []
        try:
            for candidate in candidates:
                label = _validated_label(run, candidate)
                base_time, variant_time = candidate["baseline_time_ms"], candidate["rewritten_time_ms"]
                accepted = (base_time - variant_time >= 2 and
                            (base_time - variant_time) / max(base_time, .001) >= .05)
                if candidate.get("accepted") is not accepted:
                    raise ValueError("Acceptance flag disagrees with the frozen policy thresholds")
                features.append(rewrite_features(run["original_sql"], candidate["variant_sql"],
                                                 candidate["rule_id"], candidate["estimated_plans"]))
                # Use only estimated plan fields, never actual execution time.
                from .plan_features import extract_plan_features
                base = extract_plan_features(candidate["estimated_plans"]["baseline"]).estimated_total_cost
                variant = extract_plan_features(candidate["estimated_plans"]["variant"]).estimated_total_cost
                costs.append(math.log(max(base, 1e-6) / max(variant, 1e-6)))
                entries.append({"rule_id": candidate["rule_id"], "label": label,
                                "accepted": accepted,
                                "baseline_time_ms": candidate["baseline_time_ms"],
                                "variant_time_ms": candidate["rewritten_time_ms"]})
            scores = [float(value) for value in artifact["model"].predict(artifact["vectorizer"].transform(features))]
            if len(scores) != len(entries) or not all(math.isfinite(value) for value in scores):
                raise ValueError("Invalid predictions")
            family = run["collection"]["family_id"]
            if not isinstance(family, str):
                raise ValueError("Missing family")
        except (ValueError, KeyError, TypeError, AttributeError, SqlglotError):
            # Never invent an oracle over only the surviving candidates.
            excluded["incomplete_or_invalid_query"] += 1
            continue
        queries.append({"run_id": run["run_id"], "family_id": family,
                        "candidates": entries, "model_scores": scores, "estimated_cost_scores": costs,
                        "choices": {"model": _pick(scores, math.log(1.05)), "generator_first": 0,
                                    "estimated_cost": _pick(costs, 0.), "noop": None}})
    metrics = summarize_queries(queries)
    metrics.update(excluded=dict(excluded), oracle="best measured accepted candidate or original",
                   replay_only=True, validates_equivalence_before_application=True)
    return {"metrics": metrics, "queries": queries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="Trusted locally trained joblib only")
    parser.add_argument("--training-journal", type=Path, required=True)
    parser.add_argument("--training-batch", type=Path, required=True, help="Frozen batch.json used for the model")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--budget-seconds", type=int, default=180)
    args = parser.parse_args()
    import joblib
    artifact = joblib.load(args.model)
    families, shapes = frozen_holdout(artifact, args.training_journal)
    batch = json.loads(args.training_batch.read_text(encoding="utf-8"))
    # Reconstruct only relationships from the trusted catalog, then match their
    # stable IDs to held-out provenance. No outcome-dependent case selection.
    from .rewrite_batch import discover_relationships
    settings = replace(DatabaseSettings.from_env(), statement_timeout_ms=1500)
    if batch["database_fingerprint"] != database_fingerprint(settings):
        raise ValueError("Ranking connection differs from the training database")
    relationships = [relation for relation in discover_relationships(settings) if relation.family_id in families["control"]]
    if {relation.family_id for relation in relationships} != families["control"]:
        raise ValueError("Held-out relationships changed since training")
    cases = ranking_cases(relationships)
    for case in cases:
        candidates = generate_sql_rewrites(case.sql)
        if any(sql_identity(sql)["shape_hash"] in shapes for sql in [case.sql, *(item.sql_text for item in candidates)]):
            raise ValueError("Ranking SQL overlaps development shapes")
    model_hash = hashlib.sha256(args.model.read_bytes()).hexdigest()
    # Persist the frozen model identity before executing any query.
    identity = {"model_sha256": model_hash, "training_dataset_sha256": artifact["dataset_report"]["dataset_sha256"]}
    if args.resume:
        if json.loads((args.output / "ranking-identity.json").read_text()) != identity:
            raise ValueError("Ranking model changed on resume")
    elif args.output.exists():
        parser.error("Output already exists")
    # collect_batch owns its private measurement directory and read-only protocol.
    args.output.mkdir(parents=True, exist_ok=args.resume)
    _save(args.output / "ranking-identity.json", identity)
    state = collect_batch(cases, settings, args.output / "measurements", max_cases=500,
                          budget_seconds=args.budget_seconds, resume=args.resume)
    result = replay_ranking(artifact, args.output / "measurements/journal", families["control"])
    result.update(identity=identity, collection_status=state["status"],
                  note="New combined shapes on prior held-out relationships, not wholly unseen databases")
    if state["status"] != "completed":
        result["metrics"]["passes_ranking_gate"] = False
    _save(args.output / "ranking-report.json", result)
    print(json.dumps(result["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
