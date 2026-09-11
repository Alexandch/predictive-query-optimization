"""Collect and evaluate sealed sequential control workloads."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .chbenchmark_control_queries import CHBenchmarkControlQueryGenerator
from .logistics_control_queries import LogisticsControlQueryGenerator
from .sequential_dqn import evaluate_sequential_dqn_control
from .sequential_experience import collect_sequential_experience
from .sequential_rollout import evaluate_sequential_rollout


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential DQN sealed control")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("domain", choices=("logistics", "chbenchmark"))
    collect.add_argument("count", type=int)
    collect.add_argument("output", type=Path)
    collect.add_argument("--seed", type=int)
    collect.add_argument("--repetitions", type=int, default=1)
    collect.add_argument("--max-steps", type=int, default=2)
    collect.add_argument("--budget-mb", type=float, default=64.0)
    collect.add_argument("--resume", action="store_true")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("experience", type=Path)
    evaluate.add_argument("model", type=Path)
    evaluate.add_argument("output_dir", type=Path)
    evaluate.add_argument("--seed", type=int, default=42)
    rollout = commands.add_parser("rollout")
    rollout.add_argument("domain", choices=("logistics", "chbenchmark"))
    rollout.add_argument("experience", type=Path)
    rollout.add_argument("model", type=Path)
    rollout.add_argument("output_dir", type=Path)
    rollout.add_argument("--seed", type=int, default=42)
    rollout.add_argument("--repetitions", type=int, default=1)
    rollout.add_argument("--max-steps", type=int, default=2)
    rollout.add_argument("--budget-mb", type=float, default=64.0)
    args = parser.parse_args()

    if args.command == "evaluate":
        metrics = evaluate_sequential_dqn_control(
            args.experience, args.model, args.output_dir, seed=args.seed
        )
        print(json.dumps(asdict(metrics), ensure_ascii=False, indent=2))
        return 0

    if args.command == "rollout":
        metrics = evaluate_sequential_rollout(
            args.experience,
            args.model,
            args.output_dir,
            allowed_schemas=frozenset({args.domain}),
            repetitions=args.repetitions,
            max_steps=args.max_steps,
            storage_budget_bytes=round(args.budget_mb * 1024 * 1024),
            random_seed=args.seed,
        )
        print(json.dumps(asdict(metrics), ensure_ascii=False, indent=2))
        return 0

    if args.count <= 0:
        parser.error("count must be positive")
    if args.budget_mb <= 0:
        parser.error("budget-mb must be positive")
    settings = {
        "logistics": (LogisticsControlQueryGenerator, 10401, "logistics"),
        "chbenchmark": (CHBenchmarkControlQueryGenerator, 16101, "chbenchmark"),
    }
    generator_type, default_seed, schema = settings[args.domain]
    seed = args.seed if args.seed is not None else default_seed

    def progress(position: int, total: int, template: str) -> None:
        print(f"[{position}/{total}] {template}", flush=True)

    count = collect_sequential_experience(
        generator_type(seed=seed).generate(args.count),
        args.output,
        allowed_schemas=frozenset({schema}),
        seed=seed,
        repetitions=args.repetitions,
        max_steps=args.max_steps,
        storage_budget_bytes=round(args.budget_mb * 1024 * 1024),
        resume=args.resume,
        progress=progress,
    )
    print(f"Collected {count} sealed {args.domain} transitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
