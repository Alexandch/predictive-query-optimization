"""CLI for database-specific query-time calibration."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .calibration import calibrate_query, load_profile


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate an XGBoost model for the connected PostgreSQL database"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="execute one SELECT and add its timing")
    add.add_argument("model", type=Path)
    add.add_argument("profile", type=Path)
    add.add_argument("sql")
    status = commands.add_parser("status", help="show a calibration profile")
    status.add_argument("profile", type=Path)
    args = parser.parse_args()

    if args.command == "status":
        profile = load_profile(args.profile)
        result = {
            **asdict(profile),
            "sample_count": profile.sample_count,
            "unique_query_count": profile.unique_query_count,
            "ready": profile.ready,
            "factor": profile.factor,
            "base_mae_ms": profile.base_mae_ms,
            "calibrated_mae_ms": profile.calibrated_mae_ms,
        }
    else:
        calibration = calibrate_query(args.sql, args.model, args.profile)
        result = {
            "observation": asdict(calibration.observation),
            "sample_count": calibration.profile.sample_count,
            "unique_query_count": calibration.profile.unique_query_count,
            "ready": calibration.profile.ready,
            "factor": calibration.profile.factor,
            "base_mae_ms": calibration.profile.base_mae_ms,
            "calibrated_mae_ms": calibration.profile.calibrated_mae_ms,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
