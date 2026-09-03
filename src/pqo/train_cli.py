"""CLI for training the XGBoost query-time model."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .training import train_xgboost


def main() -> int:
    parser = argparse.ArgumentParser(description="Train and evaluate the XGBoost model.")
    parser.add_argument("dataset", type=Path, help="Collected CSV dataset")
    parser.add_argument("output_dir", type=Path, help="Model artifact directory")
    parser.add_argument("--seed", type=int, default=42, help="Split and model seed")
    parser.add_argument(
        "--no-tune",
        action="store_true",
        help="Use baseline hyperparameters without cross-validated search",
    )
    parser.add_argument(
        "--target-transform",
        choices=("log1p", "identity"),
        default="log1p",
        help="Regression target transformation",
    )
    args = parser.parse_args()

    metrics = train_xgboost(
        args.dataset,
        args.output_dir,
        random_state=args.seed,
        tune=not args.no_tune,
        target_transform=args.target_transform,
    )
    print(json.dumps(asdict(metrics), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
