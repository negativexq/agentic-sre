"""M19-1.2 migration tests for persisted Kubernetes instance identity."""

from datetime import datetime
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.engine import Engine

from packages.storage.models import EventVersionRow, ObjectVersionRow

PREVIOUS_REVISION = "0015_investigation_run_artifacts"
TARGET_REVISION = "0016_instance_identity"


def _migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _assert_identity_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    object_columns = {column["name"]: column for column in inspector.get_columns("object_versions")}
    event_columns = {column["name"]: column for column in inspector.get_columns("event_versions")}
    assert cast(String, object_columns["uid"]["type"]).length == 64
    assert object_columns["uid"]["nullable"] is True
    assert cast(String, event_columns["involved_uid"]["type"]).length == 64
    assert event_columns["involved_uid"]["nullable"] is True

    indexes = {item["name"]: item for item in inspector.get_indexes("object_versions")} | {
        item["name"]: item for item in inspector.get_indexes("event_versions")
    }
    assert indexes["ix_object_versions_namespace_kind_name_uid"]["column_names"] == [
        "namespace",
        "kind",
        "name",
        "uid",
    ]
    assert not indexes["ix_object_versions_namespace_kind_name_uid"].get("unique", False)
    assert indexes["ix_event_versions_namespace_involved_kind_name_involved_uid"][
        "column_names"
    ] == ["namespace", "involved_kind", "involved_name", "involved_uid"]
    assert not indexes["ix_event_versions_namespace_involved_kind_name_involved_uid"].get(
        "unique", False
    )


def test_instance_identity_migration_backfills_and_reverses_on_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "instance-identity.db"
    config = _migration_config(database_path)
    command.upgrade(config, PREVIOUS_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    # Revision 0001 creates tables from live metadata, so strip the M19 fields
    # here to model a database that was already at revision 0015 before M19-1.1.
    with engine.begin() as connection:
        existing_indexes = {
            index["name"]: table_name
            for table_name in ("object_versions", "event_versions")
            for index in inspect(connection).get_indexes(table_name)
        }
        for index_name in (
            "ix_object_versions_namespace_kind_name_uid",
            "ix_event_versions_namespace_involved_kind_name_involved_uid",
        ):
            if index_name in existing_indexes:
                connection.exec_driver_sql(f"DROP INDEX {index_name}")
        for table_name, column_name in (
            ("object_versions", "uid"),
            ("event_versions", "involved_uid"),
        ):
            columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
            if column_name in columns:
                connection.exec_driver_sql(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
    schema_inspector = inspect(engine)
    assert "uid" not in {
        column["name"] for column in schema_inspector.get_columns("object_versions")
    }
    assert "involved_uid" not in {
        column["name"] for column in schema_inspector.get_columns("event_versions")
    }

    with engine.begin() as connection:
        table_metadata = MetaData()
        object_versions = Table("object_versions", table_metadata, autoload_with=connection)
        event_versions = Table("event_versions", table_metadata, autoload_with=connection)
        connection.execute(
            object_versions.insert(),
            [
                {
                    "object_key": "Pod/sre-demo/payment-service-abc",
                    "namespace": "sre-demo",
                    "kind": "Pod",
                    "name": "payment-service-abc",
                    "observed_at": datetime(2026, 9, 25, 12, 0, 0),
                    "content_hash": "explicit-uid",
                    "body": {"metadata": {"name": "payment-service-abc", "uid": "pod-uid-A"}},
                },
                {
                    "object_key": "Pod/sre-demo/legacy-pod",
                    "namespace": "sre-demo",
                    "kind": "Pod",
                    "name": "legacy-pod",
                    "observed_at": datetime(2026, 9, 25, 12, 1, 0),
                    "content_hash": "missing-uid",
                    "body": {"metadata": {"name": "legacy-pod"}},
                },
            ],
        )
        connection.execute(
            event_versions.insert(),
            [
                {
                    "namespace": "sre-demo",
                    "involved_kind": "Pod",
                    "involved_name": "payment-service-abc",
                    "dedup_key": "event-explicit-uid",
                    "event_at": datetime(2026, 9, 25, 12, 0, 0),
                    "observed_at": datetime(2026, 9, 25, 12, 0, 1),
                    "body": {
                        "metadata": {"name": "event-1"},
                        "involvedObject": {
                            "kind": "Pod",
                            "namespace": "sre-demo",
                            "name": "payment-service-abc",
                            "uid": "pod-uid-A",
                        },
                    },
                },
                {
                    "namespace": "sre-demo",
                    "involved_kind": "Pod",
                    "involved_name": "legacy-pod",
                    "dedup_key": "event-missing-uid",
                    "event_at": datetime(2026, 9, 25, 12, 1, 0),
                    "observed_at": datetime(2026, 9, 25, 12, 1, 1),
                    "body": {
                        "metadata": {"name": "event-2"},
                        "involvedObject": {"kind": "Pod", "name": "legacy-pod"},
                    },
                },
            ],
        )

    command.upgrade(config, TARGET_REVISION)
    with engine.connect() as connection:
        metadata = MetaData()
        object_versions = Table("object_versions", metadata, autoload_with=connection)
        event_versions = Table("event_versions", metadata, autoload_with=connection)
        object_uids = connection.execute(
            select(object_versions.c.name, object_versions.c.uid).order_by(object_versions.c.name)
        ).all()
        event_uids = connection.execute(
            select(event_versions.c.involved_name, event_versions.c.involved_uid).order_by(
                event_versions.c.involved_name
            )
        ).all()
    assert [(row[0], row[1]) for row in object_uids] == [
        ("legacy-pod", None),
        ("payment-service-abc", "pod-uid-A"),
    ]
    assert [(row[0], row[1]) for row in event_uids] == [
        ("legacy-pod", None),
        ("payment-service-abc", "pod-uid-A"),
    ]
    _assert_identity_schema(engine)
    engine.dispose()

    # The mapped storage models expose the nullable columns without changing repository behavior.
    assert cast(String, ObjectVersionRow.__table__.c.uid.type).length == 64
    assert ObjectVersionRow.__table__.c.uid.nullable is True
    assert cast(String, EventVersionRow.__table__.c.involved_uid.type).length == 64
    assert EventVersionRow.__table__.c.involved_uid.nullable is True

    command.downgrade(config, PREVIOUS_REVISION)
    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    assert "uid" not in {column["name"] for column in inspector.get_columns("object_versions")}
    assert "involved_uid" not in {
        column["name"] for column in inspector.get_columns("event_versions")
    }
    assert "ix_object_versions_namespace_kind_name_uid" not in {
        index["name"] for index in inspector.get_indexes("object_versions")
    }
    assert "ix_event_versions_namespace_involved_kind_name_involved_uid" not in {
        index["name"] for index in inspector.get_indexes("event_versions")
    }
    engine.dispose()

    command.upgrade(config, TARGET_REVISION)
    engine = create_engine(f"sqlite:///{database_path}")
    _assert_identity_schema(engine)
    with engine.connect() as connection:
        table_metadata = MetaData()
        object_versions = Table("object_versions", table_metadata, autoload_with=connection)
        event_versions = Table("event_versions", table_metadata, autoload_with=connection)
        object_uids = connection.execute(
            select(object_versions.c.name, object_versions.c.uid).order_by(object_versions.c.name)
        ).all()
        event_uids = connection.execute(
            select(event_versions.c.involved_name, event_versions.c.involved_uid).order_by(
                event_versions.c.involved_name
            )
        ).all()
        assert [(row[0], row[1]) for row in object_uids] == [
            ("legacy-pod", None),
            ("payment-service-abc", "pod-uid-A"),
        ]
        assert [(row[0], row[1]) for row in event_uids] == [
            ("legacy-pod", None),
            ("payment-service-abc", "pod-uid-A"),
        ]
    engine.dispose()
