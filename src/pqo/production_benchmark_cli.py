"""CLI for collecting and evaluating the independent production-like workload."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .control_benchmark import evaluate_dqn_control, evaluate_xgboost_control
from .dataset import collect_to_csv
from .dqn_experience import collect_dqn_case_experience
from .production_queries import ProductionQueryGenerator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent production-like control benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect-xgb", help="measure unseen queries for XGBoost")
    collect.add_argument("count", type=int)
    collect.add_argument("output", type=Path)
    collect.add_argument("--seed", type=int, default=7301)
    collect.add_argument("--repetitions", type=int, default=2)
    collect.add_argument("--resume", action="store_true")

    dqn = commands.add_parser("collect-dqn", help="measure index actions on unseen queries")
    dqn.add_argument("count", type=int)
    dqn.add_argument("output", type=Path)
    dqn.add_argument("--seed", type=int, default=7301)
    dqn.add_argument("--actions-per-query", type=int, default=2)
    dqn.add_argument("--all-actions", action="store_true")
    dqn.add_argument("--repetitions", type=int, default=1)
    dqn.add_argument("--resume", action="store_true")

    evaluate = commands.add_parser("evaluate", help="evaluate frozen models")
    evaluate.add_argument("xgb_dataset", type=Path)
    evaluate.add_argument("xgb_model", type=Path)
    evaluate.add_argument("output_dir", type=Path)
    evaluate.add_argument("--dqn-experience", type=Path)
    evaluate.add_argument("--dqn-model", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    generator = ProductionQueryGenerator(seed=getattr(args, "seed", 7301))
    if args.command == "collect-xgb":
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

        def progress(position, total, result):
            print(f"[{completed + position}/{completed + total}] {result.values['template_id']}", flush=True)

        results = collect_to_csv(
            cases,
            args.output,
            persist=False,
            progress=progress,
            append=completed > 0,
        )
        print(f"Collected {completed + len(results)} XGBoost control measurements")
        return 0

    if args.command == "collect-dqn":
        cases = generator.generate(args.count)

        def progress(position, total, template_id):
            print(f"[{position}/{total}] {template_id}", flush=True)

        count = collect_dqn_case_experience(
            cases,
            args.output,
            actions_per_query=None if args.all_actions else args.actions_per_query,
            repetitions=args.repetitions,
            seed=args.seed,
            resume=args.resume,
            progress=progress,
        )
        print(f"Collected {count} DQN control records")
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
