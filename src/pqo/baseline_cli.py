"""Create or verify the frozen training baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .training_baseline import create_baseline, verify_baseline


DEFAULT_MANIFEST = Path("models/baseline/manifest.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze and verify a training baseline")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    check = commands.add_parser("check")
    check.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    root = Path.cwd()
    if args.command == "create":
        manifest = create_baseline(root, args.output)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    errors = verify_baseline(root, args.manifest)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
