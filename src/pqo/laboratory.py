"""Create a fresh baseline and working copy; never overwrite or terminate sessions."""

from dataclasses import dataclass, replace
import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import sql

from .analysis_control import AnalysisCancelled, AnalysisControl
from .transaction_context import read_transaction_options

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,55}$")


@dataclass(frozen=True)
class Laboratory:
    source_database: str
    working_database: str
    reference_database: str
    identities: tuple[tuple[str, int, int], ...]
    schemas: tuple[str, ...]
    recovery_manifest: str | None = None


class LaboratoryError(RuntimeError):
    def __init__(self, message, created=()):
        self.created = tuple(created)
        super().__init__(
            message
            + (
                " Созданные базы сохранены для проверки: "
                + ", ".join(x[0] for x in created)
                + "."
                if created
                else ""
            )
        )


def postgres_tools(directory=None):
    suffix = ".exe" if os.name == "nt" else ""
    roots = []
    if directory:
        roots.append(Path(directory))
    else:
        found = shutil.which("pg_dump" + suffix)
        if found:
            roots.append(Path(found).parent)
        if os.name == "nt":
            parent = (
                Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PostgreSQL"
            )
            if parent.is_dir():
                roots.extend(
                    p / "bin"
                    for p in sorted(
                        parent.iterdir(),
                        key=lambda x: int(x.name) if x.name.isdigit() else -1,
                        reverse=True,
                    )
                )
    for root in roots:
        pair = tuple(root / (name + suffix) for name in ("pg_dump", "pg_restore"))
        if all(p.is_file() for p in pair):
            return pair
    raise LaboratoryError(
        "Не найдены pg_dump и pg_restore. Установите клиентские утилиты PostgreSQL или укажите их папку bin."
    )


def _tool_env(settings):
    # Never inherit a service/connection override from the parent process.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    env.update(
        PGHOST=settings.host,
        PGPORT=str(settings.port),
        PGDATABASE=settings.dbname,
        PGUSER=settings.user,
        PGPASSWORD=settings.password,
        PGCONNECT_TIMEOUT=str(settings.connect_timeout),
        PGAPPNAME="pqo_prepare_laboratory",
        PGOPTIONS="-c statement_timeout=0 -c lock_timeout=5000",
    )
    if settings.sslmode:
        env["PGSSLMODE"] = settings.sslmode
    if settings.sslrootcert:
        env["PGSSLROOTCERT"] = settings.sslrootcert
    return env


def _run_tool(command, settings, error_file, control, deadline):
    with error_file.open("wb") as errors:
        process = subprocess.Popen(
            [str(x) for x in command],
            env=_tool_env(settings),
            stdout=subprocess.DEVNULL,
            stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while process.poll() is None:
                control.check()
                if time.monotonic() > deadline:
                    raise TimeoutError("Подготовка копии превысила лимит 20 минут.")
                time.sleep(0.1)
            control.check()
            if process.returncode:
                # Tool diagnostics can contain source row values or executable SQL.
                raise LaboratoryError(
                    f"{Path(command[0]).stem} не завершил копирование. Проверьте права, зависимости выбранных схем и версию утилит."
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def prepare_laboratory(
    settings,
    working_database,
    *,
    destination_settings=None,
    tools_directory=None,
    control=None,
    recovery_directory=None,
):
    """Dump selected schemas once, restore into a NEW baseline, clone working DB.

    Restoring is trusted-source DDL, not the guarded transaction executor. Both
    destinations must be new names on the destination server. Partial databases
    are retained on error/cancellation; there is no DROP, FORCE or reuse here.
    """
    control = control or AnalysisControl()
    created = []
    reference = working_database + "_base"
    if (
        not NAME.fullmatch(working_database)
        or working_database.lower() in {"postgres", "template0", "template1"}
        or settings.dbname.casefold()
        in {working_database.casefold(), reference.casefold()}
        or any(
            s in {"pqo", "pg_catalog", "information_schema"} or s.startswith("pg_")
            for s in settings.allowed_schemas
        )
    ):
        raise LaboratoryError(
            "Укажите новое имя копии до 56 ASCII-символов и пользовательские схемы; исходная и служебная БД не перезаписываются."
        )
    dump, restore = postgres_tools(tools_directory)
    deadline = time.monotonic() + 1200
    destination = destination_settings or settings
    admin = replace(destination, dbname="postgres")
    options = (
        "-c statement_timeout=30000 -c lock_timeout=1000 -c search_path=pg_catalog"
    )
    work_dir = None
    manifest = None
    if recovery_directory is not None:
        folder = Path(recovery_directory)
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / ("laboratory-" + uuid4().hex + ".json")

    def record(pending=None, completed=False):
        if manifest is None:
            return
        data = {
            "host": destination.host,
            "port": destination.port,
            "user": destination.user,
            "source": settings.dbname,
            "working": working_database,
            "reference": reference,
            "created": created,
            "pending": pending,
            "completed": completed,
        }
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(manifest)

    try:
        control.report("Проверяю подключение, выбранные схемы и свободные имена копий…")
        with (
            psycopg.connect(**settings.connection_kwargs(), autocommit=True) as source,
            control.monitor(source),
        ):
            modes = read_transaction_options(source, defaults=True)
            existing = {
                row[0]
                for row in source.execute(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname=ANY(%s)",
                    (sorted(settings.allowed_schemas),),
                )
            }
            if existing != set(settings.allowed_schemas):
                raise LaboratoryError(
                    "Не все выбранные схемы найдены. Загрузите список схем заново."
                )
        with psycopg.connect(
            **admin.connection_kwargs(), autocommit=True, options=options
        ) as connection:
            if not connection.execute(
                "SELECT rolcreatedb OR rolsuper FROM pg_catalog.pg_roles WHERE rolname=current_user"
            ).fetchone()[0]:
                raise LaboratoryError(
                    "Для создания стенда роли нужно право CREATEDB. Используйте тестовый сервер с нужными правами."
                )
            if connection.execute(
                "SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s)",
                ([working_database, reference],),
            ).fetchall():
                raise LaboratoryError(
                    "Это имя копии или эталона уже занято. Выберите другое имя; существующие базы не изменены."
                )
        work_dir = Path(tempfile.mkdtemp(prefix="pqo-copy-"))
        archive, error_file = work_dir / "source.dump", work_dir / "tool-error.log"
        control.report(
            "Создаю согласованный снимок выбранных схем: исходная БД продолжает работать…"
        )
        command = [
            dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-tablespaces",
            "--no-password",
            "--lock-wait-timeout=5000",
            "--file",
            archive,
        ]
        for schema in sorted(settings.allowed_schemas):
            command.append("--schema=" + sql.Identifier(schema).as_string())
        _run_tool(command, settings, error_file, control, deadline)
        control.report("Создаю новый эталон для сравнения транзакций…")
        with psycopg.connect(
            **admin.connection_kwargs(), autocommit=True, options=options
        ) as connection:
            record(pending=reference)
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(reference)
                )
            )
            oid, owner = connection.execute(
                "SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s",
                (reference,),
            ).fetchone()
            created.append((reference, oid, owner))
            record()
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(reference)
                )
            )
        target = replace(
            destination, dbname=reference, allowed_schemas=settings.allowed_schemas
        )
        if "public" in settings.allowed_schemas:
            with psycopg.connect(
                **target.connection_kwargs(), autocommit=True, options=options
            ) as connection:
                connection.execute(
                    "DROP SCHEMA public"
                )  # only our new, empty template0 database; no CASCADE
        control.report("Восстанавливаю структуру и данные в новом эталоне…")
        _run_tool(
            [
                restore,
                "--no-owner",
                "--no-privileges",
                "--no-tablespaces",
                "--no-password",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                reference,
                archive,
            ],
            target,
            error_file,
            control,
            deadline,
        )
        with (
            psycopg.connect(
                **target.connection_kwargs(), autocommit=True, options=options
            ) as connection,
            control.monitor(connection),
        ):
            connection.execute("ANALYZE")
        control.report("Создаю рабочую копию для отдельных запросов…")
        with psycopg.connect(
            **admin.connection_kwargs(), autocommit=True, options=options
        ) as connection:
            # CREATE DATABASE does not inherit database-specific default GUCs.
            for name, value in (
                ("default_transaction_isolation", modes.isolation_level.lower()),
                ("default_transaction_read_only", "on" if modes.read_only else "off"),
                ("default_transaction_deferrable", "on" if modes.deferrable else "off"),
            ):
                connection.execute(
                    sql.SQL("ALTER DATABASE {} SET {} TO {}").format(
                        sql.Identifier(reference),
                        sql.Identifier(name),
                        sql.Literal(value),
                    )
                )
            record(pending=working_database)
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(working_database), sql.Identifier(reference)
                )
            )
            oid, owner = connection.execute(
                "SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s",
                (working_database,),
            ).fetchone()
            created.append((working_database, oid, owner))
            record()
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(working_database)
                )
            )
            for name, value in (
                ("default_transaction_isolation", modes.isolation_level.lower()),
                ("default_transaction_read_only", "on" if modes.read_only else "off"),
                ("default_transaction_deferrable", "on" if modes.deferrable else "off"),
            ):
                connection.execute(
                    sql.SQL("ALTER DATABASE {} SET {} TO {}").format(
                        sql.Identifier(working_database),
                        sql.Identifier(name),
                        sql.Literal(value),
                    )
                )
        control.check()
        record(completed=True)
        return Laboratory(
            settings.dbname,
            working_database,
            reference,
            tuple(created),
            tuple(sorted(settings.allowed_schemas)),
            str(manifest) if manifest else None,
        )
    except (
        AnalysisCancelled,
        TimeoutError,
        LaboratoryError,
        psycopg.Error,
        OSError,
    ) as exc:
        if isinstance(exc, AnalysisCancelled):
            message = "Подготовка стенда отменена. Готовый стенд не зарегистрирован."
        elif isinstance(exc, LaboratoryError):
            message = str(exc)
        elif isinstance(exc, TimeoutError):
            message = str(exc)
        elif getattr(exc, "sqlstate", None) == "42501":
            message = "Недостаточно прав. Для создания стенда роли нужны CREATEDB и доступ к выбранным схемам."
        else:
            message = "Стенд не подготовлен. Проверьте подключение, права и свободное место; исходная БД не перезаписывалась."
        raise LaboratoryError(message, created) from None
    finally:
        if work_dir is not None:
            # Only explicitly named files in this freshly allocated directory.
            for name in ("source.dump", "tool-error.log"):
                path = work_dir / name
                if path.is_file():
                    path.unlink()
            work_dir.rmdir()
