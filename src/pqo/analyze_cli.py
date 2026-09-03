"""CLI for the combined XGBoost and DQN application service."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .analysis_service import analyze_query


def main() -> int:
    parser = argparse.ArgumentParser(description="Predict and recommend in one call")
    parser.add_argument("xgboost_model", type=Path)
    parser.add_argument("dqn_model", type=Path)
    parser.add_argument("sql")
    parser.add_argument("--threshold-ms", type=float, default=50.0)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    result = analyze_query(
        args.sql,
        args.xgboost_model,
        args.dqn_model,
        recommendation_threshold_ms=args.threshold_ms,
        persist=not args.no_persist,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
