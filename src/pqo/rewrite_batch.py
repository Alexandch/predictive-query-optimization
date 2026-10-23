"""Bounded read-only collection of paired rewrite experiments, not training."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .config import DatabaseSettings
from .rewrite_journal import database_fingerprint, error_record
from .sql_rewrite import evaluate_sql_rewrites


@dataclass(frozen=True)
class Relationship:
    child_schema: str
    child_table: str
    child_key: str
    parent_schema: str
    parent_table: str
    parent_key: str

    @property
    def family_id(self):
        # All rules and literal settings on this relationship stay together.
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BatchCase:
    case_id: str
    family_id: str
    role: str
    template: str
    sql: str


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def discover_relationships(settings):
    """Catalog-only discovery of real numeric single-column FK relationships."""
    import psycopg
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SELECT set_config('statement_timeout', %s, true)",
                               (f"{settings.statement_timeout_ms}ms",))
            rows = connection.execute("""
                SELECT cn.nspname, ct.relname, ca.attname,
                       pn.nspname, pt.relname, pa.attname
                FROM pg_constraint AS fk
                JOIN pg_class AS ct ON ct.oid = fk.conrelid
                JOIN pg_namespace AS cn ON cn.oid = ct.relnamespace
                JOIN pg_class AS pt ON pt.oid = fk.confrelid
                JOIN pg_namespace AS pn ON pn.oid = pt.relnamespace
                JOIN pg_attribute AS ca ON ca.attrelid = ct.oid AND ca.attnum = fk.conkey[1]
                JOIN pg_attribute AS pa ON pa.attrelid = pt.oid AND pa.attnum = fk.confkey[1]
                WHERE fk.contype = 'f' AND cardinality(fk.conkey) = 1
                  AND cardinality(fk.confkey) = 1
                  AND ca.atttypid IN (20, 21, 23) AND pa.atttypid IN (20, 21, 23)
                  AND cn.nspname = ANY(%s) AND pn.nspname = ANY(%s)
                  AND ct.relkind IN ('r', 'p') AND pt.relkind IN ('r', 'p')
                ORDER BY cn.nspname, ct.reltuples DESC, ct.relname, ca.attname,
                         pn.nspname, pt.relname, pa.attname
                """, (sorted(settings.allowed_schemas), sorted(settings.allowed_schemas))).fetchall()
    return [Relationship(*row) for row in rows]


def build_cases(relationships, *, parent_limit=8, key_upper=256):
    if not 1 <= parent_limit <= 64 or not 4 <= key_upper <= 10000:
        raise ValueError("Unsafe collection bounds")
    cases = []
    for relation in sorted(set(relationships), key=lambda item: tuple(asdict(item).values())):
        child = f"{_identifier(relation.child_schema)}.{_identifier(relation.child_table)}"
        parent = f"{_identifier(relation.parent_schema)}.{_identifier(relation.parent_table)}"
        ck, pk = _identifier(relation.child_key), _identifier(relation.parent_key)
        parent_sample = f"(SELECT {pk} FROM {parent} ORDER BY {pk} LIMIT {parent_limit}) AS p"
        templates = {
            "count-positive": f"SELECT p.{pk} FROM {parent_sample} WHERE (SELECT COUNT(*) FROM {child} AS c WHERE c.{ck} = p.{pk}) > 0 ORDER BY p.{pk}",
            "count-zero": f"SELECT p.{pk} FROM {parent_sample} WHERE (SELECT COUNT(*) FROM {child} AS c WHERE c.{ck} = p.{pk}) = 0 ORDER BY p.{pk}",
            "or-equalities": f"SELECT c.{ck}, COUNT(*) AS n FROM {child} AS c WHERE c.{ck} = 1 OR c.{ck} = 2 OR c.{ck} = 3 GROUP BY c.{ck} ORDER BY c.{ck}",
            "range": f"SELECT c.{ck}, COUNT(*) AS n FROM {child} AS c WHERE c.{ck} >= 1 AND c.{ck} <= {key_upper} GROUP BY c.{ck} ORDER BY c.{ck}",
            "inner-sort": f"SELECT q.{ck}, COUNT(*) AS n FROM (SELECT c.{ck} FROM {child} AS c WHERE c.{ck} BETWEEN 1 AND {key_upper} ORDER BY c.{ck} DESC) AS q GROUP BY q.{ck} ORDER BY q.{ck}",
            "having": f"SELECT c.{ck}, COUNT(*) AS n FROM {child} AS c WHERE c.{ck} BETWEEN 1 AND {key_upper} GROUP BY c.{ck} HAVING c.{ck} > 16 ORDER BY c.{ck}",
            "reused-cte": f"WITH subset AS (SELECT c.{ck} FROM {child} AS c WHERE c.{ck} BETWEEN 1 AND {key_upper}) SELECT {ck}, COUNT(*) AS n FROM subset GROUP BY {ck} UNION ALL SELECT {ck}, -COUNT(*) AS n FROM subset GROUP BY {ck} ORDER BY 1, 2",
        }
        role = "control" if int(relation.family_id[:8], 16) % 4 == 0 else "development"
        for template, sql in templates.items():
            identity = hashlib.sha256((relation.family_id + template + sql).encode()).hexdigest()
            cases.append(BatchCase(identity, relation.family_id, role, template, sql))
    # Interleave schemas, relationships AND rules instead of filling the budget
    # with seven almost identical queries from the first table in the catalog.
    buckets = {}
    for case in cases:
        buckets.setdefault(case.template, []).append(case)
    result = []
    for offset in range(max((len(bucket) for bucket in buckets.values()), default=0)):
        for index, bucket in enumerate(buckets.values()):
            if offset < len(bucket):
                result.append(bucket[(offset + index) % len(bucket)])
    return result


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".batch-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_limits(settings, max_cases, budget_seconds, repetitions):
    if not 1 <= max_cases <= 500 or not 1 <= budget_seconds <= 3600 or not 3 <= repetitions <= 7:
        raise ValueError("Invalid batch limits")
    if not 100 <= settings.statement_timeout_ms <= 10000:
        raise ValueError("Batch statement timeout must be between 100 and 10000 ms")


def _validate_cases(cases, settings):
    from sqlglot import exp, parse_one
    from .explain import _assert_read_only_query
    ids = set()
    for case in cases:
        if case.case_id in ids or case.role not in {"development", "control"}:
            raise ValueError("Invalid or duplicate batch case")
        ids.add(case.case_id)
        tree = parse_one(_assert_read_only_query(case.sql), read="postgres")
        ctes = {node.alias_or_name for node in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.db not in settings.allowed_schemas and not (not table.db and table.name in ctes):
                raise ValueError("Batch SQL references a schema outside the configured allowlist")


@contextmanager
def _batch_lock(output):
    # Kernel file locks release on process death; no stale PID/lock deletion.
    with (Path(output) / ".session.lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("This batch is already running in another process") from exc
        yield


def collect_batch(cases, settings, output, *, max_cases=28, budget_seconds=180,
                  repetitions=3, resume=False):
    """Case-granularity budget; each statement also has a server timeout."""
    _validate_limits(settings, max_cases, budget_seconds, repetitions)
    cases = list(cases)
    _validate_cases(cases, settings)
    output = Path(output)
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
    with _batch_lock(output):
        return _collect_batch_unlocked(cases, settings, output, max_cases=max_cases,
                                       budget_seconds=budget_seconds, repetitions=repetitions, resume=resume)


def _collect_batch_unlocked(cases, settings, output, *, max_cases, budget_seconds,
                            repetitions, resume):
    output = Path(output)
    fingerprint = database_fingerprint(settings)
    payload_cases = [asdict(case) for case in cases]
    suite_digest = hashlib.sha256(json.dumps(payload_cases, sort_keys=True).encode()).hexdigest()
    state_path = output / "batch.json"
    if resume:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (state.get("suite_sha256") != suite_digest or state.get("database_fingerprint") != fingerprint
                or state.get("repetitions") != repetitions
                or state.get("statement_timeout_ms") != settings.statement_timeout_ms):
            raise ValueError("Resume configuration does not match saved batch")
    else:
        state = {"schema_version": 1, "batch_id": uuid4().hex,
                 "created_at": datetime.now(timezone.utc).isoformat(),
                 "database_fingerprint": fingerprint, "suite_sha256": suite_digest,
                 "repetitions": repetitions, "statement_timeout_ms": settings.statement_timeout_ms,
                 "allowed_schemas": sorted(settings.allowed_schemas),
                 "status": "running", "cases": [], "suite": payload_cases}
        _save(state_path, state)
    state["status"] = "running"
    # An interrupted case is retried in a NEW journal run; the old incomplete
    # record remains excluded. Completed/failed attempts are not reweighted.
    for entry in state["cases"]:
        if entry["status"] == "running":
            entry["status"] = "interrupted"
    done = {entry["case_id"] for entry in state["cases"] if entry["status"] != "interrupted"}
    started = time.monotonic()
    processed = 0
    manifest_path = output / "split.json"
    def checkpoint():
        _save(state_path, state)
        manifest = {f"{role}_run_ids": [entry["run_id"] for entry in state["cases"]
                                         if entry["role"] == role and entry.get("run_id")]
                    for role in ("development", "control")}
        _save(manifest_path, manifest)
    try:
        for case in cases:
            if case.case_id in done:
                continue
            if processed >= max_cases or time.monotonic() - started >= budget_seconds:
                break
            entry = {"case_id": case.case_id, "family_id": case.family_id, "role": case.role,
                     "template": case.template, "status": "running", "run_id": None}
            state["cases"].append(entry)
            checkpoint()
            try:
                plan = evaluate_sql_rewrites(
                    case.sql, settings, repetitions=repetitions,
                    minimum_baseline_time_ms=0, minimum_absolute_improvement_ms=2,
                    minimum_improvement_ratio=.05,
                    journal_directory=output / "journal", journal_source=f"batch_{case.role}",
                    journal_metadata={"batch_id": state["batch_id"], "case_id": case.case_id,
                                      "family_id": case.family_id, "template": case.template},
                )
                entry.update(status="completed", run_id=plan.journal_run_id,
                             terminal_reason=plan.terminal_reason, journal_warning=plan.journal_warning,
                             evaluations=len(plan.evaluations), accepted=sum(item.accepted for item in plan.evaluations))
            except Exception as exc:
                entry.update(status="failed", error=error_record(exc, "batch_case"))
            processed += 1
            checkpoint()
            print(json.dumps({key: entry.get(key) for key in
                              ("template", "role", "status", "terminal_reason", "evaluations", "accepted")}), flush=True)
        finished = {entry["case_id"] for entry in state["cases"] if entry["status"] in {"completed", "failed"}}
        state["status"] = "completed" if len(finished) == len(cases) else "budget_exhausted"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        raise
    finally:
        state["last_session_seconds"] = time.monotonic() - started
        checkpoint()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New batch directory, or existing with --resume")
    parser.add_argument("--schema", action="append", choices=["aviation", "retail", "logistics", "pagila", "chbenchmark"])
    parser.add_argument("--execute", action="store_true", help="Without this flag only catalog discovery and preview run")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-cases", type=int, default=28)
    parser.add_argument("--budget-seconds", type=int, default=180)
    parser.add_argument("--statement-timeout-ms", type=int, default=1500)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    schemas = frozenset(args.schema or ["aviation", "retail", "logistics"])
    if not schemas <= settings.allowed_schemas:
        parser.error("Requested schemas are not allowed by connection configuration")
    settings = replace(settings, allowed_schemas=schemas, statement_timeout_ms=args.statement_timeout_ms)
    if args.resume:
        saved = json.loads((args.output / "batch.json").read_text(encoding="utf-8"))
        cases = [BatchCase(**item) for item in saved["suite"]]
    else:
        relations = discover_relationships(settings)
        # Bound the suite while retaining representatives from every schema.
        selected = []
        for schema in sorted(schemas):
            chosen, tables = [], set()
            available = [item for item in relations if item.child_schema == schema]
            for item in available:
                if item.child_table not in tables:
                    chosen.append(item)
                    tables.add(item.child_table)
                if len(chosen) == 4:
                    break
            selected.extend(chosen)
        cases = build_cases(selected)
    if not args.execute:
        if args.output.exists():
            parser.error("Preview output already exists")
        args.output.mkdir(parents=True)
        _save(args.output / "preview.json", {"cases": [asdict(case) for case in cases],
                                             "executes_queries": False})
        print(f"Preview: {len(cases)} cases, {len({case.family_id for case in cases})} relationship groups")
        return 0
    state = collect_batch(cases, settings, args.output, max_cases=args.max_cases,
                          budget_seconds=args.budget_seconds, repetitions=args.repetitions, resume=args.resume)
    print(f"Batch {state['status']}: {len(state['cases'])}/{len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
