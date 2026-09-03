"""Command-line entry points for dataset collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataset import collect_to_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect PostgreSQL EXPLAIN and SQL features into a CSV dataset."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="UTF-8 text file containing one SQL query per line",
    )
    parser.add_argument("output", type=Path, help="Destination CSV file")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not store collected plans and features in the pqo schema",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    queries = args.input.read_text(encoding="utf-8").splitlines()
    results = collect_to_csv(
        queries,
        args.output,
        persist=not args.no_persist,
    )
    print(f"Collected {len(results)} queries into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

