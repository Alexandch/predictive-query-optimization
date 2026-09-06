"""CLI for predicting one SQL query without executing it."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .prediction import predict_sql_query


def main() -> int:
    parser = argparse.ArgumentParser(description="Predict PostgreSQL query time.")
    parser.add_argument("model", type=Path, help="XGBoost joblib artifact")
    parser.add_argument("sql", help="One SELECT/WITH query")
    parser.add_argument("--calibration", type=Path, help="Compatible calibration JSON")
    args = parser.parse_args()

    result = predict_sql_query(
        args.sql,
        args.model,
        calibration_profile_path=args.calibration,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
