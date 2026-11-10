"""Atomic, optimistic status updates for saved index recommendation metadata.

No proposed DDL is executed here. In particular, accepting a candidate is not
evidence of acceleration, and a status change cannot create or remove an index.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import DatabaseSettings


class IndexRecommendationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    REVERTED = "reverted"


class RecommendationStatusConflict(ValueError):
    """The caller's expected status is stale; refresh instead of overwriting."""


@dataclass(frozen=True, slots=True)
class IndexRecommendationStatusUpdate:
    recommendation_id: int
    previous_status: str
    status: str


def set_index_recommendation_status(
    recommendation_id: int,
    expected_status: IndexRecommendationStatus | str,
    new_status: IndexRecommendationStatus | str,
    settings: DatabaseSettings | None = None,
    *,
    connection=None,
) -> IndexRecommendationStatusUpdate:
    """CALL the schema API inside a transaction/savepoint, never direct UPDATE.

    An owned connection commits on success. A supplied connection remains under
    the caller's transaction control; errors roll back only this savepoint.
    Schema installation is an explicit administrative operation, not a hidden
    side effect of recording a decision.
    """
    import psycopg

    if isinstance(recommendation_id, bool) or not isinstance(recommendation_id, int) or recommendation_id <= 0:
        raise ValueError("recommendation_id must be a positive integer")
    expected = IndexRecommendationStatus(expected_status)
    target = IndexRecommendationStatus(new_status)
    if expected == target:
        raise ValueError("Repeated status transitions are not allowed")
    settings = settings or DatabaseSettings.from_env()
    if connection is None:
        with psycopg.connect(**settings.connection_kwargs()) as owned:
            return set_index_recommendation_status(
                recommendation_id, expected, target, settings, connection=owned
            )

    try:
        with connection.transaction():
            ready = connection.execute(
                "SELECT to_regprocedure('pqo.set_recommendation_status(bigint,text,text)') IS NOT NULL"
            ).fetchone()[0]
            if not ready:
                raise RuntimeError(
                    "Процедура смены статуса отсутствует. Администратору нужно применить "
                    "database/init/013_recommendation_status.sql к служебной БД."
                )
            timeout = str(max(1, settings.statement_timeout_ms))
            previous_timeouts = connection.execute(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
            ).fetchone()
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                (timeout, str(min(max(1, int(timeout) // 2), 5000))),
            )
            connection.execute(
                "CALL pqo.set_recommendation_status(%s, %s, %s)",
                (recommendation_id, expected.value, target.value),
            )
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                previous_timeouts,
            )
    except psycopg.errors.ObjectNotInPrerequisiteState as exc:
        raise RecommendationStatusConflict(
            "Статус рекомендации уже изменён. Обновите историю и повторно выберите решение."
        ) from exc
    except psycopg.errors.NoDataFound as exc:
        raise ValueError("Индексная рекомендация не найдена. Обновите историю.") from exc
    return IndexRecommendationStatusUpdate(recommendation_id, expected.value, target.value)
