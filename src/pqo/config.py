"""Application configuration loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    dbname: str = "query_optimizer"
    user: str = "query_optimizer"
    password: str = "query_optimizer"
    host: str = "localhost"
    port: int = 55432
    connect_timeout: int = 5

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
