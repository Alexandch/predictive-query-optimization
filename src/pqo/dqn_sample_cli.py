"""Sample complete DQN query groups for augmentation-volume experiments."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dqn_experience import sample_dqn_query_groups


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample complete DQN query groups")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fraction", type=float, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    count = sample_dqn_query_groups(
        args.input,
        args.output,
        fraction=args.fraction,
        seed=args.seed,
    )
    print(f"Sampled {count} DQN experiences into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
