"""Merge DQN JSONL datasets into the current universal feature encoding."""

from __future__ import annotations

import argparse
from pathlib import Path

from .merge import merge_dqn_experience


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge and re-encode DQN experience")
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    count = merge_dqn_experience(args.inputs, args.output)
    print(f"Merged and re-encoded {count} DQN experiences into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
