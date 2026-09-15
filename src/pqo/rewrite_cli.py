"""CLI for semantics-checked and measured SQL rewrites."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from .sql_rewrite import evaluate_sql_rewrites, generate_sql_rewrites


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate, verify and measure safe PostgreSQL rewrites"
    )
    parser.add_argument("sql", help="one PostgreSQL SELECT/WITH query")
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--threshold-ms", type=float, default=50.0)
    parser.add_argument("--minimum-gain-ms", type=float, default=5.0)
    parser.add_argument("--minimum-gain-ratio", type=float, default=0.05)
    args = parser.parse_args()

    if args.generate_only:
        result = generate_sql_rewrites(args.sql)
    else:
        result = evaluate_sql_rewrites(
            args.sql,
            repetitions=args.repetitions,
            minimum_baseline_time_ms=args.threshold_ms,
            minimum_absolute_improvement_ms=args.minimum_gain_ms,
            minimum_improvement_ratio=args.minimum_gain_ratio,
        )
    payload = (
        [asdict(item) for item in result]
        if isinstance(result, tuple)
        else asdict(result)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
