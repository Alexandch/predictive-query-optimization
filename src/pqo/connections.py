"""Non-secret named PostgreSQL profiles and read-only schema discovery."""

from dataclasses import dataclass, replace
import json
import uuid
from .config import DatabaseSettings

PROFILE_KEY = "connection_profiles_v1"
SERVICE_KEY = "service_connection_v1"


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    id: str
    name: str
    host: str
    port: int
    database: str
    user: str
    schemas: tuple[str, ...] = ("public",)
    sslmode: str | None = None
    sslrootcert: str | None = None

    @classmethod
    def from_settings(cls, name, settings, *, profile_id=None):
        return cls(
            profile_id or uuid.uuid4().hex,
            name.strip(),
            settings.host,
            settings.port,
            settings.dbname,
            settings.user,
            tuple(sorted(settings.allowed_schemas)),
            settings.sslmode,
            settings.sslrootcert,
        )

    def settings(self, password=""):
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.id, str)
            or not self.id
        ):
            raise ValueError("Profile name and id are required")
        return DatabaseSettings(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=password,
            allowed_schemas=frozenset(self.schemas),
            sslmode=self.sslmode,
            sslrootcert=self.sslrootcert,
        )

    def public_dict(self):
        # Explicit allowlist: a password/DSN can never be serialized accidentally.
        return dict(
            id=self.id,
            name=self.name,
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            schemas=list(self.schemas),
            sslmode=self.sslmode,
            sslrootcert=self.sslrootcert,
        )


def encode_profiles(profiles):
    ids = [p.id for p in profiles]
    if len(ids) != len(set(ids)) or not 1 <= len(ids) <= 100:
        raise ValueError("Expected 1–100 uniquely identified profiles")
    for profile in profiles:
        profile.settings()
    return json.dumps([p.public_dict() for p in profiles], ensure_ascii=False)


def decode_profiles(raw):
    values = json.loads(raw)
    if not isinstance(values, list):
        raise ValueError("Profile storage must be a list")
    profiles = []
    fields = set(ConnectionProfile.__dataclass_fields__)
    for value in values:
        if not isinstance(value, dict) or set(value) - fields:
            raise ValueError("Invalid profile or secret field in profile storage")
        if not isinstance(value.get("schemas"), list):
            raise ValueError("Profile schemas must be a list")
        try:
            profiles.append(
                ConnectionProfile(**{**value, "schemas": tuple(value["schemas"])})
            )
        except TypeError as exc:
            raise ValueError("Incomplete connection profile") from exc
    encode_profiles(profiles)
    return profiles


def endpoint(settings):
    return (
        settings.host.lower(),
        settings.port,
        settings.dbname,
        settings.user,
        settings.sslmode,
        settings.sslrootcert,
    )


def target_context(settings):
    return dict(
        host=settings.host,
        port=settings.port,
        database=settings.dbname,
        user=settings.user,
        schemas=sorted(settings.allowed_schemas),
    )


def discover_schemas(settings):
    """Only metadata SELECTs, no user query, DDL, ANALYZE or service installation."""
    import psycopg

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout','2000',true)",
            (str(min(settings.statement_timeout_ms, 10000)),),
        )
        database = connection.execute("SELECT current_database()").fetchone()[0]
        rows = connection.execute("""
            SELECT n.nspname, count(c.oid)
            FROM pg_catalog.pg_namespace n
            LEFT JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid
                AND c.relkind IN ('r','p','v','m','f')
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                AND has_schema_privilege(n.oid,'USAGE')
            GROUP BY n.oid,n.nspname ORDER BY n.nspname
        """).fetchall()
    return database, tuple((str(name), int(count)) for name, count in rows)


SERVICE_SCRIPTS = (
    "001_core_schema.sql",
    "002_query_features.sql",
    "003_query_templates.sql",
    "004_extended_plan_features.sql",
    "005_database_context_features.sql",
    "006_sequential_analysis.sql",
    "007_structural_feedback.sql",
    "008_structural_validation.sql",
    "009_sequential_measurement_status.sql",
    "012_candidate_trial_evidence.sql",
    "013_recommendation_status.sql",
    "014_target_context.sql",
)


def prepare_service_schema(settings, *, confirmed=False):
    """Explicit service-only setup; never called by source analysis/discovery."""
    if not confirmed:
        raise ValueError(
            "Explicit confirmation of service schema installation is required"
        )
    import psycopg
    from .app_paths import resource_root

    scripts = [
        (resource_root() / "database/init" / name).read_text(encoding="utf-8-sig")
        for name in SERVICE_SCRIPTS
    ]
    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute(
            "SELECT set_config('statement_timeout','30000',true), set_config('lock_timeout','2000',true)"
        )
        exists, recognized = connection.execute(
            "SELECT to_regnamespace('pqo') IS NOT NULL, to_regclass('pqo.query_run') IS NOT NULL"
        ).fetchone()
        if exists and not recognized:
            raise ValueError(
                "An unrecognized pqo schema already exists; refusing to overwrite it"
            )
        for script in scripts:
            connection.execute(script, prepare=False)
    return f"Служебная схема pqo подготовлена в {settings.host}:{settings.port}/{settings.dbname}. Исследуемая БД не изменялась."


def check_service_connection(settings):
    import psycopg

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SELECT set_config('statement_timeout','5000',true)")
        database, ready = connection.execute("""
            SELECT current_database(), EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='pqo' AND table_name='query_run' AND column_name='target_context'
            ) AND to_regprocedure('pqo.set_recommendation_status(bigint,text,text)') IS NOT NULL
        """).fetchone()
    return (
        f"{database}: служебная история готова."
        if ready
        else f"{database}: подключение работает, но служебная схема не подготовлена. Нажмите «Подготовить служебную схему»."
    )


def create_service_database(settings, *, confirmed=False):
    """Explicit CREATE DATABASE only; never drop or take over an existing DB."""
    if not confirmed:
        raise ValueError("Explicit confirmation of database creation is required")
    import psycopg
    from psycopg import sql

    admin = replace(settings, dbname="postgres")
    with psycopg.connect(**admin.connection_kwargs(), autocommit=True) as connection:
        connection.execute(
            "SELECT set_config('statement_timeout','30000',false), set_config('lock_timeout','2000',false)"
        )
        if connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (settings.dbname,)
        ).fetchone():
            return f"{settings.dbname}: база уже существует. Проверьте её и отдельно подготовьте служебную схему."
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(settings.dbname)
            )
        )
    return f"{settings.dbname}: пустая служебная БД создана. Теперь нажмите «Подготовить служебную схему»."
