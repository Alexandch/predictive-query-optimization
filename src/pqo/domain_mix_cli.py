"""CLI for development-only domain-mixture selection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .domain_mix import evaluate_xgboost_domain_mix


def main() -> int:
    parser = argparse.ArgumentParser(description="Select an additional-domain fraction")
    parser.add_argument("base_dataset", type=Path)
    parser.add_argument("pagila_dataset", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument(
        "--fractions",
        type=float,
        nargs="+",
        default=(0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=(21, 42, 84))
    args = parser.parse_args()
    report = evaluate_xgboost_domain_mix(
        args.base_dataset,
        args.pagila_dataset,
        args.output_directory,
        fractions=args.fractions,
        seeds=args.seeds,
    )
    print(
        json.dumps(
            {
                "selected_fraction": report.selected_fraction,
                "summaries": [asdict(summary) for summary in report.summaries],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
