"""CLI for sequential multi-index transition collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from .pagila_queries import PagilaQueryGenerator
from .query_generator import AviationQueryGenerator
from .retail_queries import RetailQueryGenerator
from .sequential_experience import collect_sequential_experience


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect sequential index transitions")
    parser.add_argument("domain", choices=("aviation", "retail", "pagila"))
    parser.add_argument("count", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument("--budget-mb", type=float, default=64.0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("count must be positive")
    if args.budget_mb <= 0:
        parser.error("budget-mb must be positive")
    generators = {
        "aviation": AviationQueryGenerator,
        "retail": RetailQueryGenerator,
        "pagila": PagilaQueryGenerator,
    }
    generator = generators[args.domain](seed=args.seed)

    def progress(position: int, total: int, template: str) -> None:
        print(f"[{position}/{total}] {template}", flush=True)

    count = collect_sequential_experience(
        generator.generate(args.count),
        args.output,
        allowed_schemas=frozenset({args.domain}),
        seed=args.seed,
        repetitions=args.repetitions,
        max_steps=args.max_steps,
        storage_budget_bytes=round(args.budget_mb * 1024 * 1024),
        resume=args.resume,
        progress=progress,
    )
    print(f"Collected {count} sequential transitions into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
