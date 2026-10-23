"""Atomic local experiment records; logging must never change SQL execution results."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

SCHEMA_VERSION = 1
GENERATOR_VERSION = "rules-v1.7"


def _now():
    return datetime.now(timezone.utc).isoformat()


def sql_identity(sql):
    """Separate exact identity from literal-independent grouping for future splits."""
    from sqlglot import exp, parse_one
    try:
        tree = parse_one(sql, read="postgres")
        canonical = tree.sql(dialect="postgres", comments=False)
        shape = tree.transform(lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node)
        shape = shape.sql(dialect="postgres", comments=False)
    except Exception:
        canonical = shape = sql.strip()
    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"sql_hash": digest(canonical), "shape_hash": digest(shape)}


def database_fingerprint(settings):
    # No password, DSN, hostname or user name is stored in plaintext.
    identity = [settings.host, settings.port, settings.dbname, settings.user]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


class RewriteJournalRun:
    def __init__(self, directory, original_sql, settings, *, mode, repetitions, thresholds, source="api", metadata=None):
        self.directory = Path(directory)
        self.warning = None
        self.enabled = True
        run_id = uuid4().hex
        self.path = self.directory / f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{run_id}.json"
        self.data = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "started_at": _now(), "updated_at": _now(), "finished_at": None,
            "status": "running", "stage": "validation", "mode": mode,
            "generator_version": GENERATOR_VERSION,
            "source": source, "dataset_role": {"smoke_test": "test", "batch_development": "development",
                                                "batch_control": "control"}.get(source, "unassigned"),
            "collection": dict(metadata or {}),
            "database_fingerprint": database_fingerprint(settings),
            "statement_timeout_ms": settings.statement_timeout_ms,
            "allowed_schemas": sorted(settings.allowed_schemas),
            "original_sql": original_sql, **sql_identity(original_sql),
            "protocol": {"repetitions": repetitions, "pair_warmups": 1,
                         "order": "alternating", "thresholds": thresholds},
            "environment": {}, "candidates": [], "baseline_probe": None,
            "terminal_reason": None, "error": None,
        }
        self.flush()

    def flush(self):
        if not self.enabled:
            return
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.data["updated_at"] = _now()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".rewrite-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, ensure_ascii=False, allow_nan=False, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(4):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except (OSError, TypeError, ValueError) as exc:
            self.warning = f"Журнал не сохранён: {type(exc).__name__}: {exc}"
            self.enabled = False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def stage(self, stage):
        self.data["stage"] = stage
        self.flush()

    def candidates(self, candidates):
        self.data["candidates"] = [
            {"rule_id": item.rule_id, "title": item.title, "variant_sql": item.sql_text,
             **sql_identity(item.sql_text), "status": "pending", "stage": "pending",
             "equivalence": None, "accepted": False, "rejection_reason": None,
             "baseline_time_ms": None, "rewritten_time_ms": None,
             "improvement_ratio": None, "baseline_samples_ms": [], "variant_samples_ms": [],
             "measurements": [], "estimated_plans": {}, "error": None,
             "runtime_training_eligible": False}
            for item in candidates
        ]
        self.flush()

    def candidate_stage(self, index, stage):
        self.data["candidates"][index]["stage"] = stage
        self.data["candidates"][index]["status"] = "running"
        self.flush()

    def measurement(self, index, side, phase, time_ms, plan):
        entry = {"side": side, "phase": phase, "time_ms": time_ms, "plan": plan}
        if index is None:
            self.data["baseline_probe"] = entry
        else:
            candidate = self.data["candidates"][index]
            candidate["measurements"].append(entry)
            if phase == "measurement":
                candidate[f"{side}_samples_ms"].append(time_ms)
        self.flush()

    def evaluation(self, index, evaluation, error=None):
        candidate = self.data["candidates"][index]
        candidate.update({
            "status": "error" if error else "completed", "stage": "finished",
            "equivalence": candidate["equivalence"] if error else evaluation.equivalent,
            "accepted": evaluation.accepted, "rejection_reason": evaluation.rejection_reason,
            "baseline_time_ms": evaluation.baseline_time_ms,
            "rewritten_time_ms": evaluation.rewritten_time_ms,
            "improvement_ratio": evaluation.improvement_ratio,
            "error": error,
            "runtime_training_eligible": bool(
                not error and evaluation.equivalent
                and evaluation.baseline_time_ms is not None
                and evaluation.rewritten_time_ms is not None
                and len(candidate["baseline_samples_ms"]) == self.data["protocol"]["repetitions"]
                and len(candidate["variant_samples_ms"]) == self.data["protocol"]["repetitions"]
                and all(math.isfinite(value) and value > 0 for value in
                        candidate["baseline_samples_ms"] + candidate["variant_samples_ms"])
            ),
        })
        self.flush()

    def finish(self, plan=None, error=None):
        self.data.update(status="failed" if error else "completed", finished_at=_now(),
                         terminal_reason=plan.terminal_reason if plan else "error", error=error)
        if not error:
            self.data["stage"] = "finished"
        for candidate in self.data["candidates"]:
            if candidate["status"] == "pending":
                candidate.update(status="not_checked", rejection_reason=plan.terminal_reason if plan else "run_failed")
        self.flush()


def error_record(exc, stage):
    canceled = getattr(exc, "sqlstate", None) == "57014"
    message = str(exc)
    return {"type": type(exc).__name__, "message": str(exc)[:4000],
            "sqlstate": getattr(exc, "sqlstate", None), "stage": stage,
            "canceled": canceled, "timed_out": canceled and "statement timeout" in message.lower()}


def iter_journal(directory, *, limit=None):
    directory = Path(directory)
    if not directory.exists():
        return
    for index, path in enumerate(sorted(directory.glob("run-*.json"), reverse=True)):
        if limit is not None and index >= limit:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                    or not isinstance(data.get("run_id"), str)
                    or data.get("status") not in {"running", "completed", "failed"}
                    or not isinstance(data.get("candidates"), list)
                    or not all(isinstance(item, dict) for item in data["candidates"])):
                raise ValueError("Unsupported journal schema")
            yield data
        except (OSError, ValueError) as exc:
            yield {"status": "unreadable", "run_id": path.stem,
                   "error": {"message": str(exc)}, "candidates": []}


def journal_summary(directory, limit=200):
    rows = []
    for run in iter_journal(directory, limit=limit):
        for candidate in run.get("candidates") or [{}]:
            rows.append({
                "run_id": run["run_id"], "started_at": run.get("started_at"),
                "mode": run.get("mode"), "status": run["status"],
                "terminal_reason": run.get("terminal_reason"),
                "rule_id": candidate.get("rule_id"), "candidate_status": candidate.get("status"),
                "equivalence": candidate.get("equivalence"), "accepted": candidate.get("accepted"),
                "baseline_time_ms": candidate.get("baseline_time_ms"),
                "rewritten_time_ms": candidate.get("rewritten_time_ms"),
                "improvement_ratio": candidate.get("improvement_ratio"),
                "rejection_reason": candidate.get("rejection_reason"),
                "runtime_training_eligible": bool(run["status"] == "completed" and candidate.get("runtime_training_eligible")),
                "original_sql": run.get("original_sql"), "variant_sql": candidate.get("variant_sql"),
                "database_fingerprint": run.get("database_fingerprint"),
                "shape_hash": run.get("shape_hash"), "dataset_role": run.get("dataset_role"),
                "source": run.get("source"),
                "stage": (candidate.get("error") or run.get("error") or {}).get("stage") or candidate.get("stage") or run.get("stage"),
                "baseline_probe_ms": (run.get("baseline_probe") or {}).get("time_ms"),
                "error": candidate.get("error") or run.get("error"),
            })
    return rows


def export_rewrite_journal(directory, destination):
    """JSON retains plans; CSV is a flat inspection export, not automatic training data."""
    destination = Path(destination)
    if destination.suffix.lower() not in {".json", ".csv"}:
        raise ValueError("Choose .json or .csv")
    if destination.resolve().parent == Path(directory).resolve() and destination.name.startswith("run-"):
        raise ValueError("Cannot overwrite a journal record with an export")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if destination.suffix.lower() == ".csv" else "utf-8",
                                         newline="", dir=destination.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            if destination.suffix.lower() == ".json":
                stream.write('[\n')
                for index, run in enumerate(iter_journal(directory)):
                    if index:
                        stream.write(',\n')
                    json.dump(run, stream, ensure_ascii=False, allow_nan=False)
                stream.write('\n]')
            else:
                rows = journal_summary(directory, limit=None)
                if rows:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    for row in rows:
                        # Spreadsheet-safe inspection export; raw SQL remains available in JSON.
                        cleaned = {key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else
                                         json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)
                                   for key, value in row.items()}
                        writer.writerow(cleaned)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return str(destination)


def main():
    import argparse
    from .app_paths import writable_root
    parser = argparse.ArgumentParser(description="Inspect/export local SQL rewrite experiments; does not train models")
    parser.add_argument("--directory", type=Path, default=writable_root() / "artifacts/rewrite-journal")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.export:
        print(export_rewrite_journal(args.directory, args.export))
    else:
        print(json.dumps(journal_summary(args.directory, limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
