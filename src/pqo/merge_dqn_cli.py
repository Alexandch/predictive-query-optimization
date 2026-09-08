"""Merge DQN JSONL datasets into the current universal feature encoding."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import DatabaseSettings
from .dqn_features import (
    DEFAULT_ACTION_ENCODING,
    GENERIC_ACTION_ENCODING,
    GENERIC_V3_ACTION_ENCODING,
    LEGACY_ACTION_ENCODING,
)
from .merge import merge_dqn_experience


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge and re-encode DQN experience")
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument(
        "--encoding-version",
        choices=(
            LEGACY_ACTION_ENCODING,
            GENERIC_ACTION_ENCODING,
            GENERIC_V3_ACTION_ENCODING,
        ),
        default=DEFAULT_ACTION_ENCODING,
    )
    args = parser.parse_args()
    if args.encoding_version == GENERIC_V3_ACTION_ENCODING:
        import psycopg

        settings = DatabaseSettings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            count = merge_dqn_experience(
                args.inputs,
                args.output,
                encoding_version=args.encoding_version,
                connection=connection,
            )
    else:
        count = merge_dqn_experience(
            args.inputs,
            args.output,
            encoding_version=args.encoding_version,
        )
    print(f"Merged and re-encoded {count} DQN experiences into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
