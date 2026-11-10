"""Application configuration loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    dbname: str = "query_optimizer"
    user: str = "query_optimizer"
    password: str = "query_optimizer"
    host: str = "localhost"
    port: int = 55432
    connect_timeout: int = 5
    statement_timeout_ms: int = 30_000
    sslmode: str | None = None
    sslrootcert: str | None = None
    allowed_schemas: frozenset[str] = frozenset(
        {"aviation", "retail", "logistics", "chbenchmark", "pagila"}
    )

    def __post_init__(self) -> None:
        if self.sslrootcert is not None and not isinstance(self.sslrootcert, str):
            raise ValueError('SSL root certificate must be a file path')
        if self.sslmode not in {None, 'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'}:
            raise ValueError('Unsupported PostgreSQL SSL mode')
        for name in ("dbname", "user", "host"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Database {name} must not be empty")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("Database port must be between 1 and 65535")
        if not self.allowed_schemas:
            raise ValueError("At least one allowed schema is required")
        if any(not isinstance(schema,str) or not _IDENTIFIER.fullmatch(schema) for schema in self.allowed_schemas):
            raise ValueError("Allowed schemas must be safe PostgreSQL identifiers")

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        defaults = cls()
        return cls(
            dbname=os.getenv("POSTGRES_DB", defaults.dbname),
            user=os.getenv("POSTGRES_USER", defaults.user),
            password=os.getenv("POSTGRES_PASSWORD", defaults.password),
            host=os.getenv("POSTGRES_HOST", defaults.host),
            port=int(os.getenv("POSTGRES_PORT", str(defaults.port))),
            connect_timeout=int(
                os.getenv("POSTGRES_CONNECT_TIMEOUT", str(defaults.connect_timeout))
            ),
            statement_timeout_ms=int(
                os.getenv(
                    "POSTGRES_STATEMENT_TIMEOUT_MS",
                    str(defaults.statement_timeout_ms),
                )
            ),
            sslmode=os.getenv("POSTGRES_SSLMODE") or None,
            sslrootcert=os.getenv("POSTGRES_SSLROOTCERT") or None,
            allowed_schemas=frozenset(
                schema.strip()
                for schema in os.getenv(
                    "PQO_ALLOWED_SCHEMAS",
                    ",".join(sorted(defaults.allowed_schemas)),
                ).split(",")
                if schema.strip()
            ),
        )

    def connection_kwargs(self) -> dict[str, str | int]:
        result = {
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "connect_timeout": self.connect_timeout,
        }
        if self.sslmode:
            result['sslmode'] = self.sslmode
        if self.sslrootcert:
            result['sslrootcert'] = self.sslrootcert
        return result
