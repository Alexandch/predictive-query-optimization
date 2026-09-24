from pathlib import Path
import tempfile

import pytest

from pqo.managed_indexes import (
    ManagedIndex,
    ManagedIndexDeployment,
    load_deployment,
    save_deployment,
)


def test_deployment_round_trip_preserves_safe_rollback_record():
    deployment = ManagedIndexDeployment(
        database_identity="localhost:55432/query_optimizer",
        sql_text="SELECT 1",
        indexes=(
            ManagedIndex(
                schema_name="aviation",
                index_name="pqo_managed_0123456789ab_01234567",
                table_name="flights",
                key_columns=("departure_airport",),
                include_columns=("scheduled_departure",),
            ),
        ),
        baseline_time_ms=12.0,
        indexed_time_ms=5.0,
    )
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "active.json"
        save_deployment(deployment, target)

        restored = load_deployment(target)

    assert restored == deployment
    assert restored.improvement_ratio == pytest.approx(7 / 12)
    assert restored.drop_statements == (
        'DROP INDEX IF EXISTS "aviation"."pqo_managed_0123456789ab_01234567";',
    )


def test_deployment_rejects_unmanaged_index_name():
    deployment = ManagedIndexDeployment(
        database_identity="localhost:55432/query_optimizer",
        sql_text="SELECT 1",
        indexes=(
            ManagedIndex(
                schema_name="aviation",
                index_name="users_existing_index",
                table_name="flights",
                key_columns=("departure_airport",),
                include_columns=(),
            ),
        ),
        baseline_time_ms=12.0,
        indexed_time_ms=5.0,
    )
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "active.json"
        save_deployment(deployment, target)
        with pytest.raises(ValueError, match="unsafe index name"):
            load_deployment(target)
