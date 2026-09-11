"""CLI for merging and training sequential DQN experience."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .sequential_dqn import merge_sequential_experience, train_sequential_dqn


def merge_main() -> int:
    parser = argparse.ArgumentParser(description="Merge sequential JSONL datasets")
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    count = merge_sequential_experience(args.inputs, args.output)
    print(f"Merged {count} transitions into {args.output}")
    return 0


def train_main() -> int:
    parser = argparse.ArgumentParser(description="Train the sequential Bellman DQN")
    parser.add_argument("experience", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--epochs", type=int, default=800)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--gamma", type=float, default=0.95)
    parser.add_argument("--tau", type=float, default=0.02)
    parser.add_argument("--ranking-weight", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    metrics = train_sequential_dqn(
        args.experience,
        args.output_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        gamma=args.gamma,
        tau=args.tau,
        ranking_weight=args.ranking_weight,
        seed=args.seed,
    )
    print(json.dumps(asdict(metrics), ensure_ascii=False, indent=2))
    return 0
