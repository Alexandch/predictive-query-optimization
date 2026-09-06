"""Collect and evaluate the CH-benCHmark-derived workload."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .chbenchmark_control_queries import CHBenchmarkControlQueryGenerator
from .chbenchmark_queries import CHBenchmarkQueryGenerator
from .control_benchmark import evaluate_dqn_control, evaluate_xgboost_control
from .dataset import collect_to_csv
from .dqn_experience import (
    collect_dqn_case_experience,
    normalize_unused_index_rewards,
)


def _add_collection_arguments(command, *, default_seed: int) -> None:
    command.add_argument("count", type=int)
    command.add_argument("output", type=Path)
    command.add_argument("--seed", type=int, default=default_seed)
    command.add_argument("--repetitions", type=int, default=2)
    command.add_argument("--resume", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CH-benCHmark-derived PostgreSQL training and sealed control"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    _add_collection_arguments(
        commands.add_parser("collect-xgb", help="collect training measurements"),
        default_seed=15101,
    )
    dqn = commands.add_parser("collect-dqn", help="collect training index rewards")
    _add_collection_arguments(dqn, default_seed=15101)
    dqn.add_argument("--actions-per-query", type=int, default=2)
    dqn.add_argument("--all-actions", action="store_true")

    _add_collection_arguments(
        commands.add_parser(
            "collect-control-xgb", help="collect sealed unseen-template measurements"
        ),
        default_seed=16101,
    )
    control_dqn = commands.add_parser(
        "collect-control-dqn", help="collect sealed unseen-template index rewards"
    )
    _add_collection_arguments(control_dqn, default_seed=16101)
    control_dqn.add_argument("--actions-per-query", type=int, default=2)
    control_dqn.add_argument("--all-actions", action="store_true")

    evaluate = commands.add_parser("evaluate", help="evaluate models on sealed control")
    evaluate.add_argument("xgb_dataset", type=Path)
    evaluate.add_argument("xgb_model", type=Path)
    evaluate.add_argument("output_dir", type=Path)
    evaluate.add_argument("--dqn-experience", type=Path)
    evaluate.add_argument("--dqn-model", type=Path)
    normalize = commands.add_parser(
        "normalize-dqn",
        help="create a training copy with timing-noise rewards normalized",
    )
    normalize.add_argument("input", type=Path)
    normalize.add_argument("output", type=Path)
    return parser


def _collect_xgb(args, generator) -> int:
    cases = [
        case
        for case in generator.generate(args.count)
        for _ in range(args.repetitions)
    ]
    completed = 0
    if args.resume and args.output.is_file():
        import csv

        with args.output.open(newline="", encoding="utf-8") as stream:
            completed = sum(1 for _ in csv.DictReader(stream))
        cases = cases[completed:]

    def progress(position, total, result) -> None:
        print(
            f"[{completed + position}/{completed + total}] "
            f"{result.values['template_id']}",
            flush=True,
        )

    results = collect_to_csv(
        cases,
        args.output,
        persist=False,
        progress=progress,
        append=completed > 0,
    )
    print(f"Collected {completed + len(results)} CH XGBoost measurements")
    return 0


def _collect_dqn(args, generator) -> int:
    def progress(position, total, template_id) -> None:
        print(f"[{position}/{total}] {template_id}", flush=True)

    count = collect_dqn_case_experience(
        generator.generate(args.count),
        args.output,
        actions_per_query=None if args.all_actions else args.actions_per_query,
        repetitions=args.repetitions,
        seed=args.seed,
        resume=args.resume,
        progress=progress,
        allowed_schemas=frozenset({"chbenchmark"}),
    )
    print(f"Collected {count} CH DQN records")
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.command == "collect-xgb":
        return _collect_xgb(args, CHBenchmarkQueryGenerator(seed=args.seed))
    if args.command == "collect-dqn":
        return _collect_dqn(args, CHBenchmarkQueryGenerator(seed=args.seed))
    if args.command == "collect-control-xgb":
        return _collect_xgb(args, CHBenchmarkControlQueryGenerator(seed=args.seed))
    if args.command == "collect-control-dqn":
        return _collect_dqn(args, CHBenchmarkControlQueryGenerator(seed=args.seed))
    if args.command == "normalize-dqn":
        count = normalize_unused_index_rewards(args.input, args.output)
        print(f"Normalized {count} CH DQN records into {args.output}")
        return 0

    xgb_metrics = evaluate_xgboost_control(
        args.xgb_dataset, args.xgb_model, args.output_dir
    )
    result = {"xgboost": asdict(xgb_metrics)}
    if bool(args.dqn_experience) != bool(args.dqn_model):
        raise ValueError("--dqn-experience and --dqn-model must be used together")
    if args.dqn_experience:
        result["dqn"] = asdict(
            evaluate_dqn_control(
                args.dqn_experience, args.dqn_model, args.output_dir
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
