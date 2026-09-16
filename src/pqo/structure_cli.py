"""CLI for structural SQL and PostgreSQL-plan recommendations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from .explain import collect_explain
from .structural_advisor import analyze_query_structure


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recommend aggregation, JOIN, sort and materialized-view changes"
    )
    parser.add_argument("sql", help="one PostgreSQL SELECT/WITH query")
    parser.add_argument(
        "--sql-only",
        action="store_true",
        help="skip PostgreSQL EXPLAIN and use syntax rules only",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="execute the read-only query through EXPLAIN ANALYZE to detect spills",
    )
    parser.add_argument("--predicted-time-ms", type=float)
    args = parser.parse_args()

    plan_json = None
    if args.sql_only and args.analyze:
        parser.error("--sql-only and --analyze cannot be combined")
    if not args.sql_only:
        plan_json = collect_explain(args.sql, analyze=args.analyze).plan_json
    recommendations = analyze_query_structure(
        args.sql,
        plan_json=plan_json,
        predicted_time_ms=args.predicted_time_ms,
    )
    print(
        json.dumps(
            [asdict(item) for item in recommendations],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
