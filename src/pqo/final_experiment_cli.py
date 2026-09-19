"""Command line entry point for the frozen final experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .final_experiment import PROJECT_ROOT, run_final_experiment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the final PQO evaluation without retraining models"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "models" / "final_experiment",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    summary = run_final_experiment(
        output_dir=args.output_dir,
        repetitions=args.repetitions,
    )
    structural = summary["structural_validation"]
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir.resolve()),
                "measured": structural["automatically_measured_count"],
                "accepted": structural["accepted_count"],
                "all_trials_rolled_back": structural["all_trials_rolled_back"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
