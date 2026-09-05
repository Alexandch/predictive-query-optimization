"""Collect and evaluate the unseen logistics database control."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .control_benchmark import evaluate_dqn_control, evaluate_xgboost_control
from .dataset import collect_to_csv
from .dqn_experience import collect_dqn_case_experience
from .logistics_control_queries import LogisticsControlQueryGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description="Logistics leave-one-database-out benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    xgb = commands.add_parser("collect-xgb")
    xgb.add_argument("count", type=int)
    xgb.add_argument("output", type=Path)
    xgb.add_argument("--seed", type=int, default=10401)
    xgb.add_argument("--repetitions", type=int, default=2)
    dqn = commands.add_parser("collect-dqn")
    dqn.add_argument("count", type=int)
    dqn.add_argument("output", type=Path)
    dqn.add_argument("--seed", type=int, default=10401)
    dqn.add_argument("--actions-per-query", type=int, default=2)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("xgb_dataset", type=Path)
    evaluate.add_argument("xgb_model", type=Path)
    evaluate.add_argument("output_dir", type=Path)
    evaluate.add_argument("--dqn-experience", type=Path)
    evaluate.add_argument("--dqn-model", type=Path)
    args = parser.parse_args()
    generator = LogisticsControlQueryGenerator(seed=getattr(args, "seed", 10401))

    if args.command == "collect-xgb":
        cases = [case for case in generator.generate(args.count) for _ in range(args.repetitions)]
        results = collect_to_csv(cases, args.output, persist=False)
        print(f"Collected {len(results)} logistics zero-shot measurements")
        return 0
    if args.command == "collect-dqn":
        count = collect_dqn_case_experience(
            generator.generate(args.count),
            args.output,
            actions_per_query=args.actions_per_query,
            seed=args.seed,
            allowed_schemas=frozenset({"logistics"}),
        )
        print(f"Collected {count} logistics zero-shot DQN records")
        return 0

    metrics = evaluate_xgboost_control(args.xgb_dataset, args.xgb_model, args.output_dir)
    result = {"xgboost": asdict(metrics)}
    if bool(args.dqn_experience) != bool(args.dqn_model):
        raise ValueError("--dqn-experience and --dqn-model must be used together")
    if args.dqn_experience:
        result["dqn"] = asdict(
            evaluate_dqn_control(args.dqn_experience, args.dqn_model, args.output_dir)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
