"""CLI for non-mutating DQN index recommendations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .recommendation import recommend_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Recommend an index with DQN")
    parser.add_argument("model", type=Path)
    parser.add_argument("sql")
    args = parser.parse_args()
    result = recommend_index(args.sql, args.model)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
