"""Read-only environment fingerprint for reproducible runtime collection."""

import os
import platform

from .config import DatabaseSettings


PLANNER_SETTINGS = (
    "server_version", "shared_buffers", "work_mem", "hash_mem_multiplier", "jit",
    "max_parallel_workers_per_gather", "max_parallel_workers", "max_worker_processes",
    "effective_cache_size", "random_page_cost", "seq_page_cost", "cpu_tuple_cost",
    "cpu_index_tuple_cost", "cpu_operator_cost", "track_io_timing",
)


def capture_environment(settings=None):
    import psycopg

    settings = settings or DatabaseSettings.from_env()
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT name,setting,unit FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
                (list(PLANNER_SETTINGS),),
            ).fetchall()
            relations = connection.execute("""SELECT n.nspname,c.relname,c.reltuples,c.relpages
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p') ORDER BY 1,2""",
                (sorted(settings.allowed_schemas),)).fetchall()
    return {
        "client_platform": platform.platform(), "client_cpu_count": os.cpu_count(),
        "database": {"host": settings.host, "port": settings.port, "name": settings.dbname},
        "settings": {name: {"value": value, "unit": unit} for name, value, unit in rows},
        "relation_estimates": [{"schema": schema, "table": table, "rows": rows, "pages": pages}
                               for schema, table, rows, pages in relations],
        "measurement": {"target": "root Actual Total Time", "unit": "ms", "warmups": 1,
                        "repetitions": 3, "aggregation": "median", "analyze": True,
                        "buffers": True, "timing": "PostgreSQL EXPLAIN default",
                        "statement_timeout_ms": settings.statement_timeout_ms},
    }
