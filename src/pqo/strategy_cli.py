"""Command-line workflow for the optimization-strategy selector."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .dqn_features import build_query_state
from .index_actions import generate_index_actions
from .query_case import QueryCase
from .strategy import (
    TRAINING_SCHEMAS,
    build_strategy_features,
    collect_strategy_experience,
    predict_strategy,
    summarize_strategy_experience,
    train_strategy_classifier,
)
from .strategy_queries import generate_strategy_workload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect and train the NOOP/index/rewrite strategy selector"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    seed_workload = commands.add_parser(
        "seed-workload", help="write a reproducible three-domain workload"
    )
    seed_workload.add_argument("output", type=Path)
    seed_workload.add_argument("--ordinary-per-domain", type=int, default=6)
    seed_workload.add_argument("--rewrite-variants", type=int, default=3)
    seed_workload.add_argument("--seed", type=int, default=42)

    collect = commands.add_parser("collect", help="measure a JSONL workload")
    collect.add_argument("workload", type=Path)
    collect.add_argument("output", type=Path)
    collect.add_argument("--actions-per-query", type=int, default=6)
    collect.add_argument("--repetitions", type=int, default=1)
    collect.add_argument("--seed", type=int, default=42)
    collect.add_argument("--resume", action="store_true")

    train = commands.add_parser("train", help="train the strategy classifier")
    train.add_argument("experience", type=Path)
    train.add_argument("output_directory", type=Path)
    train.add_argument("--seed", type=int, default=42)

    summary = commands.add_parser("summary", help="show dataset balance")
    summary.add_argument("experience", type=Path)

    predict = commands.add_parser("predict", help="classify one SQL query")
    predict.add_argument("model", type=Path)
    predict.add_argument("sql")

    args = parser.parse_args()
    if args.command == "seed-workload":
        cases = generate_strategy_workload(
            ordinary_per_domain=args.ordinary_per_domain,
            rewrite_variants=args.rewrite_variants,
            seed=args.seed,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(
                json.dumps(asdict(case), ensure_ascii=False) + "\n" for case in cases
            ),
            encoding="utf-8",
        )
        result = {"query_count": len(cases), "output": str(args.output)}
    elif args.command == "collect":
        cases = _read_workload(args.workload)
        count = collect_strategy_experience(
            cases,
            args.output,
            actions_per_query=args.actions_per_query,
            repetitions=args.repetitions,
            seed=args.seed,
            resume=args.resume,
            progress=lambda current, total, name: print(
                f"[{current}/{total}] {name}", flush=True
            ),
        )
        result = {"record_count": count, "output": str(args.output)}
    elif args.command == "train":
        result = asdict(
            train_strategy_classifier(
                args.experience, args.output_directory, seed=args.seed
            )
        )
    elif args.command == "summary":
        result = summarize_strategy_experience(args.experience)
    else:
        state = build_query_state(args.sql)
        index_count = len(
            generate_index_actions(
                args.sql, allowed_schemas=TRAINING_SCHEMAS
            )
        ) - 1
        features = build_strategy_features(args.sql, state, index_count)
        result = predict_strategy(args.model, features)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _read_workload(path: Path) -> list[QueryCase]:
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        raise ValueError("Workload is empty")
    return [QueryCase(str(row["template_id"]), str(row["sql_text"])) for row in rows]


if __name__ == "__main__":
    raise SystemExit(main())
