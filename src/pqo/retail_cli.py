"""Collect XGBoost and DQN training data from the retail workload."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataset import collect_to_csv
from .dqn_experience import collect_dqn_case_experience
from .retail_queries import RetailQueryGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect retail training data")
    commands = parser.add_subparsers(dest="command", required=True)
    xgb = commands.add_parser("collect-xgb")
    xgb.add_argument("count", type=int)
    xgb.add_argument("output", type=Path)
    xgb.add_argument("--seed", type=int, default=8401)
    xgb.add_argument("--repetitions", type=int, default=1)
    xgb.add_argument("--append", action="store_true")
    dqn = commands.add_parser("collect-dqn")
    dqn.add_argument("count", type=int)
    dqn.add_argument("output", type=Path)
    dqn.add_argument("--seed", type=int, default=8401)
    dqn.add_argument("--actions-per-query", type=int, default=2)
    dqn.add_argument("--all-actions", action="store_true")
    dqn.add_argument("--repetitions", type=int, default=1)
    dqn.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    generator = RetailQueryGenerator(seed=args.seed)

    def progress(position: int, total: int, value) -> None:
        template = value.values["template_id"] if hasattr(value, "values") else value
        print(f"[{position}/{total}] {template}", flush=True)

    if args.command == "collect-xgb":
        cases = [
            case
            for case in generator.generate(args.count)
            for _ in range(args.repetitions)
        ]
        results = collect_to_csv(
            cases,
            args.output,
            persist=False,
            progress=progress,
            append=args.append,
        )
        print(f"Collected {len(results)} retail XGBoost measurements")
        return 0

    count = collect_dqn_case_experience(
        generator.generate(args.count),
        args.output,
        actions_per_query=None if args.all_actions else args.actions_per_query,
        repetitions=args.repetitions,
        seed=args.seed,
        resume=args.resume,
        progress=progress,
        allowed_schemas=frozenset({"retail"}),
    )
    print(f"Collected {count} retail DQN experiences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

