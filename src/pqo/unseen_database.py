"""Provision an isolated synthetic holdout domain and diagnose it without learning."""
from dataclasses import asdict, replace
from pathlib import Path
import argparse
import hashlib
import json
from time import monotonic

from .config import DatabaseSettings
from .prediction import predict_sql_query
from .explain import measure_query_execution
from .sql_rewrite import evaluate_sql_rewrites
from .sequential_recommendation import recommend_sequential_indexes

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "pqo_university_unseen"


def queries():
    return {
        "primary_key": "SELECT student_id,full_name FROM university.students WHERE student_id=17001",
        "index": "SELECT student_id,full_name,enrolled_at FROM university.students WHERE campus_id=7 ORDER BY enrolled_at DESC LIMIT 200",
        "enrollment_index": "SELECT enrollment_id,registered_at,status FROM university.enrollments WHERE student_id=17001 ORDER BY registered_at DESC",
        "rewrite": "SELECT s.student_id FROM university.students s WHERE s.student_id BETWEEN 1000 AND 1019 AND (SELECT COUNT(*) FROM university.enrollments e WHERE e.student_id=s.student_id)>0 ORDER BY s.student_id",
        "grouped_join": "SELECT c.department_id,o.term,COUNT(DISTINCT e.student_id) AS students,COUNT(*) AS submissions,ROUND(AVG(s.score),2) AS average_score FROM university.courses c JOIN university.offerings o ON o.course_id=c.course_id JOIN university.enrollments e ON e.offering_id=o.offering_id JOIN university.submissions s ON s.enrollment_id=e.enrollment_id WHERE e.status<>'withdrawn' GROUP BY c.department_id,o.term ORDER BY average_score DESC,c.department_id,o.term LIMIT 100",
        "window": "WITH scores AS (SELECT e.student_id,o.course_id,AVG(s.score) AS score FROM university.enrollments e JOIN university.offerings o ON o.offering_id=e.offering_id JOIN university.submissions s ON s.enrollment_id=e.enrollment_id WHERE e.status='completed' GROUP BY e.student_id,o.course_id), ranked AS (SELECT student_id,course_id,score,DENSE_RANK() OVER (PARTITION BY course_id ORDER BY score DESC) AS place FROM scores) SELECT student_id,course_id,ROUND(score,2) AS score,place FROM ranked WHERE place<=10 ORDER BY course_id,place,student_id LIMIT 500",
    }


def provision(settings):
    import psycopg
    from psycopg import sql
    target = replace(settings, dbname=DATABASE, allowed_schemas=frozenset({"university"}))
    with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise FileExistsError(f"Database {DATABASE} already exists; nothing overwritten")
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(DATABASE)))
    # No DROP or destructive recovery: failed initialization leaves the new DB for inspection.
    with psycopg.connect(**target.connection_kwargs()) as connection:
        connection.execute((ROOT / "database/unseen/university.sql").read_text(encoding="utf-8"))
        for migration in sorted((ROOT / "database/init").glob("*.sql")):
            if migration.name[:3].isdigit() and (int(migration.name[:3]) <= 9 or migration.name.startswith("012_")):
                connection.execute(migration.read_text(encoding="utf-8"))
    return target


def check(settings, model, output):
    import psycopg
    from .optimization_control import index_inventory
    settings = replace(settings, dbname=DATABASE, allowed_schemas=frozenset({"university"}), statement_timeout_ms=10000)
    output.mkdir(parents=True, exist_ok=False)
    report = {"database": DATABASE, "synthetic": True, "calibration_used": False,
              "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
              "note": "unseen schema, not unseen SQL operator families; no training or tuning",
              "queries": {}, "status": "running"}
    before = index_inventory(settings)
    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with psycopg.connect(**settings.connection_kwargs()) as connection:
            report["row_counts"] = {name: connection.execute(
                f"SELECT count(*) FROM university.{name}"
            ).fetchone()[0] for name in ("students", "courses", "offerings", "enrollments", "submissions")}
        # Freeze ALL predictions before any actual query measurement or optimization.
        for name, query in queries().items():
            (output / f"{name}.sql").write_text(query + ";\n", encoding="utf-8")
            entry = report["queries"][name] = {"sql": query}
            started = monotonic()
            try:
                prediction = predict_sql_query(query, model, settings, calibration_profile_path=None)
                entry.update(predicted_ms=prediction.predicted_time_ms, prediction_seconds=monotonic()-started,
                             estimate_source=prediction.estimate_source)
            except Exception as exc:
                entry["prediction_error"] = f"{type(exc).__name__}: {exc}"
            save()
        for name, entry in report["queries"].items():
            print("Measuring", name, flush=True)
            try:
                measurement = measure_query_execution(entry["sql"], settings, repetitions=3, warmup_runs=1)
                entry.update(measured_ms=measurement.median_time_ms, samples_ms=measurement.samples_ms)
                if "predicted_ms" in entry:
                    entry["q_error"] = max(entry["predicted_ms"], entry["measured_ms"])/max(min(entry["predicted_ms"], entry["measured_ms"]), .001)
            except Exception as exc:
                entry["measurement_error"] = f"{type(exc).__name__}: {exc}"
            save()
        report["index"] = asdict(recommend_sequential_indexes(queries()["enrollment_index"],
            ROOT / "models/sequential_dqn/sequential_dqn_index_advisor.pt", settings,
            repetitions=3, max_steps=1, minimum_baseline_time_ms=2,
            minimum_absolute_improvement_ms=1, minimum_improvement_ratio=.05, persist=False))
        report["rewrite"] = asdict(evaluate_sql_rewrites(queries()["rewrite"], settings,
            repetitions=3, minimum_baseline_time_ms=2, minimum_absolute_improvement_ms=1,
            journal_directory=output / "rewrite-journal", journal_source="test"))
        report["status"] = ("completed_with_errors" if any(
            "prediction_error" in entry or "measurement_error" in entry
            for entry in report["queries"].values()) else "completed")
    except BaseException:
        report["status"] = "failed_or_interrupted"
        raise
    finally:
        report["index_inventory_unchanged"] = index_inventory(settings) == before
        report["model_unchanged"] = hashlib.sha256(model.read_bytes()).hexdigest() == report["model_sha256"]
        save()
    if not report["index_inventory_unchanged"] or not report["model_unchanged"]:
        raise RuntimeError("Index inventory or model changed during the diagnostic; inspect report")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "check"))
    parser.add_argument("--model", type=Path, default=ROOT / "models/xgboost/xgboost_query_time.joblib")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/university-unseen-20261030")
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    if args.action == "create":
        print("Created", provision(settings).dbname)
    else:
        print(check(settings, args.model, args.output)["status"])


if __name__ == "__main__":
    main()
