"""CLI for rebuilding a dataset from PostgreSQL query history."""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_recent_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Export recent measured queries to CSV.")
    parser.add_argument("output", type=Path, help="Destination CSV file")
    parser.add_argument("--limit", type=int, default=1000, help="Number of recent rows")
    args = parser.parse_args()

    count = export_recent_dataset(args.output, args.limit)
    print(f"Exported {count} measured queries into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

