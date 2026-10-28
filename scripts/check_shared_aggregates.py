"""Read-only local demonstration; no training, index DDL or app-history writes."""

from dataclasses import asdict
import argparse
import json
from pathlib import Path

from pqo.sql_rewrite import evaluate_sql_rewrites


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", type=Path, default=Path("examples/aviation-shared-aggregates-rewrite.sql"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/shared-aggregates-check"))
    args = parser.parse_args()
    source, destination = args.example, args.output
    destination.mkdir(parents=True, exist_ok=True)
    plan = evaluate_sql_rewrites(source.read_text(encoding="utf-8"), repetitions=3,
        journal_directory=destination / "journal", journal_source="shared-aggregates-check")
    (destination / "report.json").write_text(json.dumps(asdict(plan), indent=2, ensure_ascii=False), encoding="utf-8")
    for item in plan.evaluations:
        print(item.candidate.rule_id, "equivalent", item.equivalent,
              "ms", item.baseline_time_ms, "->", item.rewritten_time_ms,
              "accepted", item.accepted, "reason", item.rejection_reason, flush=True)
    print("Recommended:", plan.recommended.candidate.rule_id if plan.recommended else None)


if __name__ == "__main__":
    main()
