"""CLI for development-only DQN domain-mixture selection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .domain_mix import evaluate_dqn_domain_mix


def main() -> int:
    parser = argparse.ArgumentParser(description="Select the Pagila fraction for DQN")
    parser.add_argument("combined_experience", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument(
        "--fractions",
        type=float,
        nargs="+",
        default=(0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=(21, 42, 84))
    parser.add_argument("--epochs", type=int, default=700)
    args = parser.parse_args()

    def progress(position, total, fraction, seed, stage):
        print(
            f"[{position}/{total}] fraction={fraction:g}, seed={seed}: {stage}",
            flush=True,
        )

    report = evaluate_dqn_domain_mix(
        args.combined_experience,
        args.output_directory,
        fractions=args.fractions,
        seeds=args.seeds,
        epochs=args.epochs,
        progress=progress,
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
