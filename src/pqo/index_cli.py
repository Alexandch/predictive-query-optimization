"""CLI for inspecting and transactionally evaluating index actions."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from .index_actions import generate_index_actions
from .index_environment import IndexExperimentEnvironment


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List or safely evaluate index candidates for a SELECT query"
    )
    parser.add_argument("sql", help="one PostgreSQL SELECT/WITH query")
    parser.add_argument(
        "--action-index",
        type=int,
        help="candidate number to evaluate; omit to only list candidates",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()

    actions = generate_index_actions(args.sql)
    if args.action_index is None:
        print(
            json.dumps(
                [asdict(action) for action in actions],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.action_index < 0 or args.action_index >= len(actions):
        parser.error(f"action index must be between 0 and {len(actions) - 1}")

    environment = IndexExperimentEnvironment(repetitions=args.repetitions)
    result = environment.evaluate(args.sql, actions[args.action_index])
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
