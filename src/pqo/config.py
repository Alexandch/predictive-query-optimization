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
    allowed_schemas: frozenset[str] = frozenset({"aviation", "retail"})

    def __post_init__(self) -> None:
        if not self.allowed_schemas:
            raise ValueError("At least one allowed schema is required")
        if any(not _IDENTIFIER.fullmatch(schema) for schema in self.allowed_schemas):
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
        return {
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "connect_timeout": self.connect_timeout,
        }
