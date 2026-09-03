"""CLI for merging compatible collected datasets."""

from __future__ import annotations

import argparse
from pathlib import Path

from .merge import merge_datasets


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge compatible PQO CSV datasets")
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    count = merge_datasets(args.inputs, args.output)
    print(f"Merged {count} rows into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
