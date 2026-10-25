"""Offline grouped preference learning with validation before final measurement."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path

from .config import DatabaseSettings
from .rewrite_batch import BatchCase, _identifier, _save, collect_batch, discover_relationships
from .rewrite_journal import GENERATOR_VERSION, database_fingerprint, iter_journal, sql_identity
from .rewrite_learning import rewrite_features, _validated_label
from .rewrite_ranking import frozen_holdout, ranking_cases, summarize_queries
from .sql_rewrite import generate_sql_rewrites

PAIRWISE_VERSION = 1
PARAMETERS = {"objective": "rank:pairwise", "n_estimators": 160, "max_depth": 3,
              "learning_rate": .05, "random_state": 42, "n_jobs": 1}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def split_families(development, final):
    development, final = set(development), set(final)
    if development & final or len(development) < 40 or len(final) < 10:
        raise ValueError("Insufficient or overlapping family sets")
    ordered = sorted(development, key=lambda value: hashlib.sha256(("pairwise-v1:" + value).encode()).hexdigest())
    return {"train": sorted(ordered[10:]), "validation": sorted(ordered[:10]), "final": sorted(final)}


def final_cases(relationships):
    """Freeze different SQL forms before looking at any final measurements."""
    cases = []
    for relation in relationships:
        child = f"{_identifier(relation.child_schema)}.{_identifier(relation.child_table)}"
        parent = f"{_identifier(relation.parent_schema)}.{_identifier(relation.parent_table)}"
        ck, pk = _identifier(relation.child_key), _identifier(relation.parent_key)
        sqls = {
            "final-filtered-count": f"SELECT p.{pk}, (SELECT COUNT(c.{ck}) FROM {child} c WHERE c.{ck}=p.{pk} AND c.{ck}>0) AS n FROM {parent} p WHERE p.{pk}>=1 AND p.{pk}<=32 ORDER BY p.{pk}",
            "final-distinct-nonnull": f"SELECT DISTINCT p.{pk} FROM {parent} p JOIN {child} c ON c.{ck}=p.{pk} WHERE (c.{ck}=1 OR c.{ck}=2 OR c.{ck}=3) AND (p.{pk}>=1 AND p.{pk}<=64) AND c.{ck} IS NOT NULL ORDER BY p.{pk} DESC",
            "final-cte-window": f"WITH subset AS (SELECT c.{ck} FROM {child} c WHERE c.{ck}>=1 AND c.{ck}<=256), totals AS (SELECT q.{ck},COUNT(*) AS n FROM (SELECT {ck} FROM subset ORDER BY {ck} DESC) q GROUP BY q.{ck} HAVING q.{ck}>1) SELECT t.{ck},t.n,DENSE_RANK() OVER(ORDER BY t.n DESC) AS n_rank FROM totals t JOIN subset s ON s.{ck}=t.{ck} GROUP BY t.{ck},t.n ORDER BY n_rank,t.{ck}",
        }
        for template, sql in sqls.items():
            if len(generate_sql_rewrites(sql)) < 2:
                raise ValueError("Final scenario needs multiple candidates")
            cases.append(BatchCase(digest([relation.family_id, template, sql]), relation.family_id, "control", template, sql))
    return cases


def make_plan(relationships, families, reference_identity, settings):
    split = split_families(families["development"], families["control"])
    relations = {item.family_id: item for item in relationships}
    if not set().union(*map(set, split.values())) <= set(relations):
        raise ValueError("Reference relationships have changed")
    development = []
    for role in ("train", "validation"):
        for case in ranking_cases([relations[key] for key in split[role]]):
            development.append(replace(case, role="development" if role == "train" else "control"))
    final = final_cases([relations[key] for key in split["final"]])
    shapes = {role: set() for role in split}
    for role, cases in (("train", [c for c in development if c.role == "development"]),
                        ("validation", [c for c in development if c.role == "control"]), ("final", final)):
        for case in cases:
            shapes[role].update(sql_identity(sql)["shape_hash"] for sql in
                                [case.sql, *(c.sql_text for c in generate_sql_rewrites(case.sql))])
    if any(shapes[a] & shapes[b] for a, b in (("train", "validation"), ("train", "final"), ("validation", "final"))):
        raise ValueError("SQL shape leakage across planned splits")
    return {"schema_version": PAIRWISE_VERSION, "generator_version": GENERATOR_VERSION,
            "parameters": PARAMETERS, "reference": reference_identity, "families": split,
            "database_fingerprint": database_fingerprint(settings),
            "development_cases": [asdict(case) for case in development],
            "final_cases": [asdict(case) for case in final],
            "policy": {"absolute_ms": 2, "ratio": .05, "noop": True, "validation_before_final": True}}


def candidate_features(original, variant, rule, plans):
    features = rewrite_features(original, variant, rule, plans)
    features["candidate.is_noop"] = int(rule == "NOOP")
    for key, value in list(features.items()):
        other = "variant." + key.removeprefix("baseline.")
        if key.startswith("baseline.") and type(value) in (float, int) and other in features:
            features["delta." + key.removeprefix("baseline.")] = features[other] - value
    return features


def load_groups(directory, cases):
    """Only selected completed runs; never infer a partial oracle on failures."""
    state = json.loads((Path(directory) / "batch.json").read_text(encoding="utf-8"))
    selected = {entry["case_id"]: entry.get("run_id") for entry in state["cases"] if entry["status"] == "completed"}
    runs = {run["run_id"]: run for run in iter_journal(Path(directory) / "journal")}
    groups, excluded = [], Counter()
    for case in cases:
        run = runs.get(selected.get(case.case_id))
        if run is None:
            excluded["missing_completed_query"] += 1
            continue
        try:
            if run.get("source") == "smoke_test":
                raise ValueError("Smoke tests cannot enter learning or validation")
            if ((run.get("collection") or {}).get("family_id") != case.family_id
                    or run["dataset_role"] != ("development" if case.role == "development" else "control")):
                raise ValueError("Incorrect role or family")
            if sql_identity(run["original_sql"])["sql_hash"] != sql_identity(case.sql)["sql_hash"]:
                raise ValueError("Measured SQL differs from the frozen plan")
            candidates = run["candidates"]
            if len(candidates) < 2:
                raise ValueError("Need multiple candidates")
            expected = [(item.rule_id, sql_identity(item.sql_text)["sql_hash"]) for item in generate_sql_rewrites(case.sql)]
            measured = [(item["rule_id"], sql_identity(item["variant_sql"])["sql_hash"]) for item in candidates]
            if expected != measured:
                raise ValueError("Candidate list differs from the frozen generator")
            first_plan = candidates[0]["estimated_plans"]["baseline"]
            features = [candidate_features(run["original_sql"], run["original_sql"], "NOOP",
                                            {"baseline": first_plan, "variant": first_plan})]
            entries, costs, utility = [], [], [0.]
            for candidate in candidates:
                label = _validated_label(run, candidate)
                base, variant = candidate["baseline_time_ms"], candidate["rewritten_time_ms"]
                accepted = base - variant >= 2 and (base - variant) / max(base, .001) >= .05
                if candidate.get("accepted") is not accepted:
                    raise ValueError("Acceptance mismatch")
                feature = candidate_features(run["original_sql"], candidate["variant_sql"],
                                             candidate["rule_id"], candidate["estimated_plans"])
                features.append(feature)
                from .plan_features import extract_plan_features
                base_cost = extract_plan_features(candidate["estimated_plans"]["baseline"]).estimated_total_cost
                variant_cost = extract_plan_features(candidate["estimated_plans"]["variant"]).estimated_total_cost
                costs.append(math.log(max(base_cost, 1e-6) / max(variant_cost, 1e-6)))
                entries.append({"rule_id": candidate["rule_id"], "accepted": accepted, "label": label})
                utility.append(label if accepted else 0.)
            levels = {value: index for index, value in enumerate(sorted(set(utility)))}
            groups.append({"run_id": run["run_id"], "case_id": case.case_id, "family_id": case.family_id,
                           "features": features, "relevance": [levels[value] for value in utility],
                           "candidates": entries, "cost_scores": costs})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            excluded[str(exc) if isinstance(exc, ValueError) else "malformed_query"] += 1
    return groups, {"collection_status": state["status"], "excluded": dict(excluded),
                    "queries": len(groups), "families": len({g["family_id"] for g in groups})}


def evaluate(artifact, groups):
    queries = []
    for group in groups:
        scores = [float(value) for value in artifact["model"].predict(artifact["vectorizer"].transform(group["features"]))]
        if len(scores) != len(group["features"]) or not all(math.isfinite(value) for value in scores):
            raise ValueError("Invalid rank scores")
        chosen = max(range(len(scores)), key=lambda index: scores[index])
        cost = max(range(len(group["cost_scores"])), key=lambda index: group["cost_scores"][index])
        queries.append({"run_id": group["run_id"], "family_id": group["family_id"],
                        "candidates": group["candidates"], "rank_scores_with_noop_first": scores,
                        "choices": {"model": chosen - 1 if chosen else None, "generator_first": 0,
                                    "estimated_cost": cost if group["cost_scores"][cost] > 0 else None,
                                    "noop": None}})
    return {"metrics": summarize_queries(queries), "queries": queries}


def fit_pairwise(groups, validation):
    train_families = {g["family_id"] for g in groups}
    validation_families = {g["family_id"] for g in validation}
    if train_families & validation_families or len(train_families) < 30 or len(validation_families) < 10:
        raise ValueError("Need disjoint 30+ training and 10+ validation families")
    from sklearn.feature_extraction import DictVectorizer
    from xgboost import XGBRanker
    vectorizer = DictVectorizer(sparse=False)
    x = vectorizer.fit_transform([feature for group in groups for feature in group["features"]])
    y = [value for group in groups for value in group["relevance"]]
    sizes = [len(group["features"]) for group in groups]
    counts = Counter(g["family_id"] for g in groups)
    model = XGBRanker(**PARAMETERS)
    model.fit(x, y, group=sizes, sample_weight=[1 / counts[g["family_id"]] for g in groups])
    artifact = {"pairwise_version": PAIRWISE_VERSION, "parameters": PARAMETERS,
                "vectorizer": vectorizer, "model": model, "automatic_activation": False,
                "train_families": sorted(train_families), "validation_families": sorted(validation_families),
                "training_run_ids": [g["run_id"] for g in groups],
                "training_queries": len(groups),
                "informative_training_queries": sum(len(set(g["relevance"])) > 1 for g in groups),
                "informative_validation_queries": sum(len(set(g["relevance"])) > 1 for g in validation)}
    return artifact, evaluate(artifact, validation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["collect", "train", "final"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-model", type=Path)
    parser.add_argument("--reference-journal", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--budget-seconds", type=int, default=180)
    args = parser.parse_args()
    import joblib
    settings = replace(DatabaseSettings.from_env(), statement_timeout_ms=1500)
    plan_path = args.output / "plan.json"
    if args.phase == "collect":
        if args.resume:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        else:
            if not args.reference_model or not args.reference_journal:
                parser.error("collect needs the trusted local reference model and journal")
            reference = joblib.load(args.reference_model)
            families, _ = frozen_holdout(reference, args.reference_journal)
            identity = {"model_sha256": hashlib.sha256(args.reference_model.read_bytes()).hexdigest(),
                        "dataset_sha256": reference["dataset_report"]["dataset_sha256"]}
            plan = make_plan(discover_relationships(settings), families, identity, settings)
            args.output.mkdir(parents=True, exist_ok=False)
            _save(plan_path, plan)
        if plan["database_fingerprint"] != database_fingerprint(settings) or plan["generator_version"] != GENERATOR_VERSION:
            raise ValueError("Connection or generator changed")
        state = collect_batch([BatchCase(**case) for case in plan["development_cases"]], settings,
                              args.output / "development", max_cases=500,
                              budget_seconds=args.budget_seconds, resume=args.resume)
        print("Development collection:", state["status"])
        return 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if (plan["parameters"] != PARAMETERS or plan["schema_version"] != PAIRWISE_VERSION
            or plan["generator_version"] != GENERATOR_VERSION):
        raise ValueError("Frozen experimental parameters changed")
    model_path = args.output / "candidate.joblib"
    if args.phase == "train":
        if model_path.exists():
            parser.error("Candidate already exists; do not overwrite this frozen experiment")
        cases = [BatchCase(**case) for case in plan["development_cases"]]
        groups, audit = load_groups(args.output / "development", cases)
        _save(args.output / "development-audit.json", audit)
        if audit["collection_status"] != "completed":
            raise ValueError("Development measurement incomplete; resume before training")
        train = [g for g in groups if g["family_id"] in plan["families"]["train"]]
        validation = [g for g in groups if g["family_id"] in plan["families"]["validation"]]
        expected_validation = {case.case_id for case in cases if case.family_id in plan["families"]["validation"]}
        if {g["case_id"] for g in validation} != expected_validation:
            raise ValueError("Validation must contain every planned query; no partial oracle")
        artifact, result = fit_pairwise(train, validation)
        artifact.update(plan_sha256=digest(plan), dataset_sha256=digest(groups))
        joblib.dump(artifact, model_path)
        result.update(audit=audit, plan_sha256=digest(plan), dataset_sha256=digest(groups),
                      model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
                      training_queries=artifact["training_queries"],
                      informative_training_queries=artifact["informative_training_queries"],
                      informative_validation_queries=artifact["informative_validation_queries"],
                      final_measurements_permitted=result["metrics"]["passes_ranking_gate"])
        _save(args.output / "validation-report.json", result)
        print(json.dumps(result["metrics"], indent=2))
        return 0
    result = json.loads((args.output / "validation-report.json").read_text(encoding="utf-8"))
    if not result["final_measurements_permitted"]:
        _save(args.output / "final-decision.json", {"status": "blocked_by_internal_validation",
                                                   "final_measurements_started": False})
        print("Final control blocked by internal validation; no final queries executed")
        return 2
    artifact = joblib.load(model_path)
    if (artifact["plan_sha256"] != digest(plan) or result["model_sha256"] != hashlib.sha256(model_path.read_bytes()).hexdigest()
            or plan["database_fingerprint"] != database_fingerprint(settings)):
        raise ValueError("Frozen plan, model or connection changed")
    development, audit = load_groups(args.output / "development", [BatchCase(**case) for case in plan["development_cases"]])
    if audit["collection_status"] != "completed" or digest(development) != artifact["dataset_sha256"]:
        raise ValueError("Development measurements changed after model freezing")
    validation = [g for g in development if g["family_id"] in plan["families"]["validation"]]
    if not evaluate(artifact, validation)["metrics"]["passes_ranking_gate"]:
        print("Final control blocked by recomputed internal validation")
        return 2
    cases = [BatchCase(**case) for case in plan["final_cases"]]
    state = collect_batch(cases, settings, args.output / "final", max_cases=500,
                          budget_seconds=args.budget_seconds, resume=args.resume)
    groups, audit = load_groups(args.output / "final", cases)
    final = evaluate(artifact, groups)
    final.update(audit=audit, model_sha256=result["model_sha256"], prior_control_reused=False,
                 note="New SQL forms, previously held-out relationships; not wholly unseen databases")
    if state["status"] != "completed" or audit["excluded"]:
        final["metrics"]["passes_ranking_gate"] = False
    _save(args.output / "final-report.json", final)
    print(json.dumps(final["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
