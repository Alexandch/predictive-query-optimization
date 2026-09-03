"""Generate aviation benchmark queries and collect their measurements."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataset import collect_to_csv
from .query_generator import AviationQueryGenerator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate aviation SQL queries and collect a PostgreSQL dataset."
    )
    parser.add_argument("count", type=int, help="Number of instantiated queries")
    parser.add_argument("output", type=Path, help="Destination CSV file")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not store collected plans and features in the pqo schema",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    generator = AviationQueryGenerator(seed=args.seed)
    cases = generator.generate(args.count)

    def report_progress(position, total, result):
        interval = max(1, total // 20)
        if position == 1 or position == total or position % interval == 0:
            print(
                f"[{position:>{len(str(total))}}/{total}] "
                f"{result.values['template_id']}"
            )

    results = collect_to_csv(
        cases,
        args.output,
        persist=not args.no_persist,
        progress=report_progress,
    )
    print(
        f"Collected {len(results)} generated queries from "
        f"{len(generator.template_ids)} templates into {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
