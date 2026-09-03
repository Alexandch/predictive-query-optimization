"""CLI for collecting transactional PostgreSQL index experience."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dqn_experience import collect_dqn_experience


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect real rewards for DQN")
    parser.add_argument("count", type=int, help="number of generated queries")
    parser.add_argument("output", type=Path, help="destination JSONL")
    parser.add_argument("--actions-per-query", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue a partial deterministic run instead of overwriting it",
    )
    args = parser.parse_args()

    def progress(position: int, total: int, template_id: str) -> None:
        print(f"[{position:>{len(str(total))}}/{total}] {template_id}", flush=True)

    count = collect_dqn_experience(
        args.count,
        args.output,
        actions_per_query=args.actions_per_query,
        repetitions=args.repetitions,
        seed=args.seed,
        resume=args.resume,
        progress=progress,
    )
    print(f"Collected {count} experiences into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
