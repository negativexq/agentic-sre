"""M19-2.1: the exact-instance index and the lifecycle ledger schema, on SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from packages.storage.models import (
    LIFECYCLE_OBSERVATION_TYPES,
    EntityInstanceRow,
    LifecycleObservationRow,
)

PREVIOUS_REVISION = "0016_instance_identity"
TARGET_REVISION = "0017_lifecycle_ledger"
TABLES = ("entity_instances", "lifecycle_observations")
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _observation(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "evidence_id": "lifecycle:shop:Pod:uid-a:1",
        "instance_uid": "uid-a",
        "namespace": "shop",
        "kind": "Pod",
        "name": "web-0",
        "type": "READY_TRUE",
        "source_at": AT,
        "observed_at": AT,
        "ingested_at": AT,
        "source": "watch",
        "payload": {"ready": True},
    }
    return row | overrides


def _instance(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "namespace": "shop",
        "kind": "Pod",
        "name": "web-0",
        "uid": "uid-a",
        "first_observed_at": AT,
        "last_observed_at": AT,
    }
    return row | overrides


def _rejects(engine: Engine, table: type[Any], row: dict[str, Any]) -> bool:
    try:
        with engine.begin() as connection:
            connection.execute(insert(table).values(**row))
    except IntegrityError:
        return True
    return False


def _assert_ledger_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    for name, model in zip(TABLES, (EntityInstanceRow, LifecycleObservationRow), strict=True):
        migrated = {column["name"] for column in inspector.get_columns(name)}
        assert migrated == set(model.__table__.columns.keys())
    assert {
        tuple(item["column_names"]) for item in inspector.get_unique_constraints("entity_instances")
    } == {("namespace", "kind", "uid")}
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("lifecycle_observations")
    } >= {("evidence_id",), ("instance_uid", "type", "observed_at", "source")}

    with engine.begin() as connection:
        connection.execute(insert(LifecycleObservationRow).values(**_observation()))
        connection.execute(insert(EntityInstanceRow).values(**_instance()))
    # Every declared lifecycle type is accepted.
    for index, kind in enumerate(LIFECYCLE_OBSERVATION_TYPES):
        with engine.begin() as connection:
            connection.execute(
                insert(LifecycleObservationRow).values(
                    **_observation(
                        evidence_id=f"lifecycle:shop:Pod:uid-b:{index}",
                        instance_uid="uid-b",
                        type=kind,
                    )
                )
            )
    ledger = LifecycleObservationRow
    assert _rejects(engine, ledger, _observation(instance_uid="uid-z"))  # same evidence_id
    assert _rejects(engine, ledger, _observation(evidence_id="lifecycle:shop:Pod:uid-a:2"))
    assert _rejects(engine, ledger, _observation(evidence_id="x:1", type="CREATED"))
    assert _rejects(engine, EntityInstanceRow, _instance(name="web-0-renamed"))
    # Same name with another UID is a different instance.
    assert not _rejects(engine, EntityInstanceRow, _instance(uid="uid-b"))


def _run_lifecycle(url: str, strip: Callable[[Engine], None]) -> None:
    config = _config(url)
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    try:
        # Revision 0001 builds tables from live metadata; drop the ledger to
        # model a database that was already at 0016 before M19-2.1.
        strip(engine)
        assert not set(TABLES) & set(inspect(engine).get_table_names())

        command.upgrade(config, TARGET_REVISION)
        _assert_ledger_schema(engine)

        command.downgrade(config, PREVIOUS_REVISION)
        assert not set(TABLES) & set(inspect(engine).get_table_names())

        command.upgrade(config, TARGET_REVISION)
        assert set(TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _drop_ledger(engine: Engine) -> None:
    with engine.begin() as connection:
        for name in reversed(TABLES):
            connection.execute(text(f"DROP TABLE IF EXISTS {name}"))


def test_lifecycle_ledger_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run_lifecycle(f"sqlite:///{tmp_path / 'ledger.db'}", _drop_ledger)


@pytest.mark.postgres
def test_lifecycle_ledger_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run_lifecycle(postgres_url, _drop_ledger)
