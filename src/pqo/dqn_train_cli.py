"""CLI for DQN training."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .dqn import train_dqn


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the DQN index advisor")
    parser.add_argument("experience", type=Path, help="JSONL experience dataset")
    parser.add_argument("output_dir", type=Path, help="model artifact directory")
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ranking-weight", type=float, default=0.10)
    parser.add_argument(
        "--split-mode",
        choices=("parameter", "unseen-template"),
        default="parameter",
    )
    args = parser.parse_args()

    metrics = train_dqn(
        args.experience,
        args.output_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        seed=args.seed,
        split_mode=args.split_mode,
        ranking_weight=args.ranking_weight,
    )
    print(json.dumps(asdict(metrics), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
