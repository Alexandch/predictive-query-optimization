"""Own a fresh local PostgreSQL cluster and run real GUI fresh/reopen acceptance.

Requires explicit opt-in, PostgreSQL binaries and OpenSSL. Never opens the
working server or deletes data directories. The stopped fixture is kept as QA
evidence; only the one-use initdb password file is removed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, timeout=60, env=None):
    # A pg_ctl grandchild may inherit stdout on Windows. Real files avoid
    # waiting for a pipe EOF from the long-running server after pg_ctl exits.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=errors,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            errors.seek(0)
            raise RuntimeError(
                f"{Path(command[0]).name} exited {result.returncode}: "
                + errors.read().decode("utf-8", errors="replace")[-1500:]
            )
    return result


def user_state():
    """Only fingerprints leave this function, not settings, paths or passwords."""
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\PQO\PredictiveQueryOptimization"
        )
    except FileNotFoundError:
        return {"registry": None, "models": {}}
    with key:
        values = {}
        index = 0
        while True:
            try:
                name, value, kind = winreg.EnumValue(key, index)
            except OSError:
                break
            values[name] = (value, kind)
            index += 1
    result = {
        "registry": hashlib.sha256(repr(sorted(values.items())).encode()).hexdigest(),
        "models": {},
    }
    for label, path in (
        ("active", values.get("xgb_model", (None,))[0]),
        (
            "backup",
            json.loads(values.get("xgb_trial_state", ("{}",))[0]).get("backup_path"),
        ),
    ):
        if path and Path(path).is_file():
            result["models"][label] = hashlib.sha256(
                Path(path).read_bytes()
            ).hexdigest()
    return result


def source_snapshot(connection_kwargs):
    with psycopg.connect(**connection_kwargs) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        count, digest = connection.execute("""SELECT count(*),
            md5(string_agg(row_to_json(t)::text, E'\n' ORDER BY id))
            FROM warehouse.orders AS t""").fetchone()
        indexes = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='warehouse' ORDER BY indexdef"
        ).fetchall()
        assert connection.execute("SELECT to_regnamespace('pqo')").fetchone() == (None,)
        return {"count": count, "digest": digest, "indexes": indexes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--openssl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument(
        "--confirm-local-laboratory", action="store_true", required=True
    )
    args = parser.parse_args()
    binaries = {
        name: (args.postgres_bin / (name + ".exe")).resolve()
        for name in ("initdb", "pg_ctl", "postgres")
    }
    for path in (
        *binaries.values(),
        args.openssl,
        *((args.executable,) if args.executable else ()),
    ):
        if not path.is_file():
            parser.error(f"Missing executable: {path}")
    out = args.output.resolve()
    # PostgreSQL's bundled OpenSSL on Windows does not reliably open Unicode
    # private-key paths. Use a new ASCII-named temporary fixture directory;
    # this restriction is on this TLS laboratory harness, not user SQL/schema.
    if os.name == "nt" and not str(out).isascii():
        parser.error("Use an ASCII-only new output path for the Windows TLS fixture")
    out.mkdir(parents=True, exist_ok=False)
    data = out / "postgres-data"
    report = {
        "status": "running",
        "external_server_tested": False,
        "separate_computer_tested": False,
        "fresh_postgresql_cluster": True,
        "cases": [],
    }
    before_user = user_state()
    admin_password, password, reader_password = (
        secrets.token_hex(24) for _ in range(3)
    )
    password_file = out / "initdb-password.tmp"
    certificate, private_key = out / "server.crt", out / "server.key"
    started = False
    port = None
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port in {5432, 55432}:
            raise RuntimeError("Refusing a working/default PostgreSQL port")
        password_file.write_text(admin_password + "\n", encoding="utf-8")
        run(
            [
                str(binaries["initdb"]),
                "-D",
                str(data),
                "-U",
                "pqo_accept_admin",
                "--pwfile=" + str(password_file),
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        password_file.unlink()
        ssl_config = out / "openssl.cnf"
        ssl_config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
        ssl_env = {**os.environ, "OPENSSL_CONF": str(ssl_config)}
        run(
            [
                str(args.openssl),
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(private_key),
                "-out",
                str(certificate),
                "-days",
                "2",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            env=ssl_env,
        )

        def quote_path(path):
            return "'" + path.as_posix().replace("'", "''") + "'"

        (data / "postgresql.auto.conf").write_text(
            "listen_addresses='127.0.0.1'\n"
            + f"port={port}\nssl=on\n"
            + f"ssl_cert_file={quote_path(certificate)}\nssl_key_file={quote_path(private_key)}\n"
            + "max_connections=25\nshared_buffers='32MB'\nlogging_collector=off\n",
            encoding="utf-8",
        )
        (data / "pg_hba.conf").write_text(
            "hostssl all all 127.0.0.1/32 scram-sha-256\n", encoding="ascii"
        )
        run(
            [
                str(binaries["pg_ctl"]),
                "-D",
                str(data),
                "-l",
                str(out / "postgres.log"),
                "-w",
                "-t",
                "45",
                "start",
            ],
            timeout=60,
        )
        started = True
        admin = dict(
            host="127.0.0.1",
            port=port,
            dbname="postgres",
            user="pqo_accept_admin",
            password=admin_password,
            sslmode="verify-full",
            sslrootcert=str(certificate),
            connect_timeout=5,
        )
        with psycopg.connect(**admin, autocommit=True) as connection:
            version, cluster_id = connection.execute(
                "SELECT version(),system_identifier::text FROM pg_control_system()"
            ).fetchone()
            report.update(postgresql=version, cluster_id=cluster_id, port=port)
            for role, role_password, createdb in (
                ("pqo_accept_user", password, True),
                ("pqo_accept_reader", reader_password, False),
            ):
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER {} PASSWORD {}").format(
                        sql.Identifier(role),
                        sql.SQL("CREATEDB" if createdb else "NOCREATEDB"),
                        sql.Literal(role_password),
                    )
                )
            for database in ("pqo_clean_source", "pqo_clean_second"):
                connection.execute(
                    sql.SQL(
                        "CREATE DATABASE {} OWNER pqo_accept_user TEMPLATE template0"
                    ).format(sql.Identifier(database))
                )
        client = {**admin, "user": "pqo_accept_user", "password": password}
        token = uuid4().hex
        before_sources = {}
        for database, count in (
            ("pqo_clean_source", 12000),
            ("pqo_clean_second", 8000),
        ):
            kwargs = {**client, "dbname": database}
            with psycopg.connect(**kwargs) as connection:
                connection.execute("CREATE SCHEMA warehouse; CREATE SCHEMA support")
                connection.execute(
                    "CREATE TABLE warehouse.orders(id integer PRIMARY KEY,customer_id integer,bucket integer,amount numeric,status text)"
                )
                connection.execute(
                    "INSERT INTO warehouse.orders SELECT n,n%%1000,n%%40,n::numeric/10,'paid' FROM generate_series(1,%s) AS n",
                    (count,),
                )
                connection.execute(
                    "CREATE TABLE support.acceptance_identity(token text NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO support.acceptance_identity VALUES (%s)", (token,)
                )
                connection.execute(
                    "GRANT USAGE ON SCHEMA warehouse,support TO pqo_accept_reader; GRANT SELECT ON ALL TABLES IN SCHEMA warehouse,support TO pqo_accept_reader"
                )
                connection.execute("ANALYZE warehouse.orders")
            before_sources[database] = source_snapshot(kwargs)
        for label, override in (
            ("plaintext_rejected", {"sslmode": "disable"}),
            (
                "wrong_hostname_rejected",
                {"host": "wrong.invalid", "hostaddr": "127.0.0.1"},
            ),
        ):
            try:
                with psycopg.connect(**{**client, **override}):
                    raise AssertionError(label + ": invalid connection accepted")
            except psycopg.OperationalError:
                report["cases"].append(label)
        with psycopg.connect(
            **{
                **client,
                "dbname": "pqo_clean_source",
                "user": "pqo_accept_reader",
                "password": reader_password,
            }
        ) as connection:
            try:
                connection.execute(
                    "CREATE INDEX forbidden_acceptance_index ON warehouse.orders(customer_id)"
                )
                raise AssertionError("Read-only role unexpectedly created an index")
            except psycopg.errors.InsufficientPrivilege:
                connection.rollback()
                report["cases"].append("readonly_role_cannot_create_source_index")
        manifest = dict(
            version=1,
            host="127.0.0.1",
            port=port,
            token=token,
            user="pqo_accept_user",
            reader="pqo_accept_reader",
            sources=["pqo_clean_source", "pqo_clean_second"],
            service="pqo_clean_service",
            settings_file=str(out / "gui/preferences.ini"),
            certificate=str(certificate),
        )
        config_file = out / "manifest.json"
        config_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        command = (
            [str(args.executable.resolve())]
            if args.executable
            else [sys.executable, "-m", "pqo.desktop"]
        )
        env = {
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(port),
            "POSTGRES_USER": "pqo_accept_user",
            "POSTGRES_DB": manifest["sources"][0],
            "POSTGRES_PASSWORD": password,
            "POSTGRES_SSLMODE": "verify-full",
            "POSTGRES_SSLROOTCERT": str(certificate),
            "PQO_ALLOWED_SCHEMAS": "warehouse,support",
            "PQO_SETTINGS_FILE": manifest["settings_file"],
            "PQO_CLEAN_ACCEPTANCE_CONFIG": str(config_file),
            "PQO_CLEAN_ACCEPTANCE_CONFIRMED": "1",
            "PQO_CLEAN_ACCEPTANCE_PASSWORD": password,
            "PQO_CLEAN_READER_PASSWORD": reader_password,
        }
        for phase in ("fresh", "reopen"):
            app_data = out / ("gui" if phase == "fresh" else "gui-reopen")
            env.update(PQO_APP_DATA_ROOT=str(app_data), PQO_CLEAN_PHASE=phase)
            if phase == "reopen":
                env["POSTGRES_PASSWORD"] = ""
            print("GUI acceptance: " + phase, flush=True)
            result = subprocess.run(
                [*command, "--clean-profile-smoke-test"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                timeout=240,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            child_file = app_data / "artifacts/clean-profile-smoke.json"
            child = (
                json.loads(child_file.read_text(encoding="utf-8"))
                if child_file.exists()
                else {"status": "missing"}
            )
            report[phase] = child
            if result.returncode or child["status"] != "passed":
                raise RuntimeError(
                    f"GUI acceptance {phase} failed; inspect its isolated report"
                )
        for database, before in before_sources.items():
            if before != source_snapshot({**client, "dbname": database}):
                raise AssertionError(
                    "Source data/indexes changed or service objects leaked into source"
                )
        report["cases"].append("both_source_data_and_indexes_unchanged_no_pqo")
        with psycopg.connect(**{**client, "dbname": manifest["service"]}) as connection:
            sources = connection.execute(
                "SELECT target_context->>'database',count(*) FROM pqo.query_run GROUP BY 1 ORDER BY 1"
            ).fetchall()
            if sources != [("pqo_clean_second", 2), ("pqo_clean_source", 1)]:
                raise AssertionError("Central history attribution mismatch")
            report["history_counts"] = sources
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    finally:
        if password_file.exists():
            password_file.unlink()
        try:
            if started or (data / "postmaster.pid").exists():
                lines = (
                    (data / "postmaster.pid").read_text(encoding="utf-8").splitlines()
                )
                if Path(lines[1]).resolve() != data or int(lines[3]) != port:
                    raise RuntimeError(
                        "Cluster identity changed; refusing automatic stop"
                    )
                run(
                    [
                        str(binaries["pg_ctl"]),
                        "-D",
                        str(data),
                        "-m",
                        "fast",
                        "-w",
                        "-t",
                        "45",
                        "stop",
                    ],
                    timeout=60,
                )
                report["private_cluster_stopped"] = not (
                    data / "postmaster.pid"
                ).exists()
                if not report["private_cluster_stopped"]:
                    raise RuntimeError("Private cluster shutdown was not confirmed")
        except Exception as exc:
            report.update(status="failed", cleanup_error=str(exc))
        after_user = user_state()
        report["native_settings_and_models_unchanged"] = before_user == after_user
        if before_user != after_user:
            report.update(
                status="failed", preservation_error="Working settings/models changed"
            )
        text = json.dumps(report, ensure_ascii=False, indent=2)
        for secret in (admin_password, password, reader_password):
            text = text.replace(secret, "[redacted]")
        (out / "report.json").write_text(text, encoding="utf-8")
        print(
            json.dumps(
                {"status": report["status"], "report": str(out / "report.json")},
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0 if report["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
