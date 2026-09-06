"""Collect training-only DQN experience biased toward negative index actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dqn_experience import (
    collect_dqn_case_experience,
    normalize_unused_index_rewards,
)
from .dqn_negative_queries import DQNNegativeQueryGenerator


def summarize(path: str | Path) -> dict[str, int | float]:
    records = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record["query_id"], []).append(record)
    usable = [items for items in groups.values() if len(items) >= 2]
    create_rewards = [
        float(record["reward"])
        for record in records
        if record["action"]["kind"] == "create"
    ]
    noop_best = sum(max(float(item["reward"]) for item in group) <= 0 for group in usable)
    negative = sum(reward < 0 for reward in create_rewards)
    return {
        "record_count": len(records),
        "query_count": len(usable),
        "template_count": len({record["template_id"] for record in records}),
        "noop_best_count": noop_best,
        "noop_best_percent": 100.0 * noop_best / len(usable) if usable else 0.0,
        "negative_action_count": negative,
        "negative_action_percent": (
            100.0 * negative / len(create_rewards) if create_rewards else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect negative DQN training experience")
    parser.add_argument("count", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=12001)
    parser.add_argument("--actions-per-query", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument(
        "--normalized-output",
        type=Path,
        help="Write a training copy where unused indexes receive only their complexity penalty",
    )
    args = parser.parse_args()

    cases = DQNNegativeQueryGenerator(seed=args.seed).generate(args.count)
    collect_dqn_case_experience(
        cases,
        args.output,
        actions_per_query=args.actions_per_query,
        repetitions=args.repetitions,
        seed=args.seed,
        allowed_schemas=frozenset({"aviation", "retail"}),
    )
    result = {"raw": summarize(args.output)}
    if args.normalized_output:
        normalize_unused_index_rewards(args.output, args.normalized_output)
        result["normalized"] = summarize(args.normalized_output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
