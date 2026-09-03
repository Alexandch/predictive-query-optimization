"""Command-line entry points for dataset collection."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .dataset import collect_to_csv
from .query_case import QueryCase


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect PostgreSQL EXPLAIN and SQL features into a CSV dataset."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="TXT with one SQL per line or CSV with template_id,sql_text",
    )
    parser.add_argument("output", type=Path, help="Destination CSV file")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not store collected plans and features in the pqo schema",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="append compatible rows instead of overwriting the CSV",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.input.suffix.lower() == ".csv":
        with args.input.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        if not rows or not {"template_id", "sql_text"}.issubset(rows[0]):
            raise ValueError("CSV input requires template_id and sql_text columns")
        queries = [QueryCase(row["template_id"], row["sql_text"]) for row in rows]
    else:
        queries = args.input.read_text(encoding="utf-8").splitlines()
    results = collect_to_csv(
        queries,
        args.output,
        persist=not args.no_persist,
        append=args.append,
    )
    print(f"Collected {len(results)} queries into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

