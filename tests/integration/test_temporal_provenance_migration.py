"""M19-3.2: ingestion provenance columns, on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.contracts import (
    ChangeRecord,
    ChangeType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.model import LogRecord
from packages.storage.models import (
    Base,
    ChangeRecordRow,
    EventVersionRow,
    LogObservationRow,
    ObjectVersionRow,
)
from packages.storage.repositories import (
    ChangeRecordRepository,
    EventRepository,
    IncidentRepository,
    LogObservationRepository,
    ObjectVersionRepository,
)

PREVIOUS = "0017_lifecycle_ledger"
TARGET = "0018_temporal_provenance"
NEW_COLUMNS = (
    ("object_versions", "ingested_at"),
    ("object_versions", "source_at"),
    ("event_versions", "ingested_at"),
    ("change_records", "ingested_at"),
    ("log_observations", "ingested_at"),
)
OBSERVED = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CHANGED = datetime(2026, 9, 25, 11, 30, tzinfo=UTC)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _incident() -> Incident:
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="latency",
        created_at=OBSERVED,
        updated_at=OBSERVED,
    )


def _seed_pre_m19_rows(engine: Engine) -> None:
    """Rows as a database at 0017 holds them, written against its reflected schema."""
    incident = _incident()
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=engine)
        for name in ("object_versions", "event_versions", "change_records", "log_observations")
    }

    def uuid_value(value: UUID) -> object:
        return value if engine.dialect.name == "postgresql" else value.hex

    def naive(value: datetime) -> datetime:
        return value.replace(tzinfo=None)

    rows = {
        "object_versions": {
            "object_key": "shop/Pod/web-0",
            "namespace": "shop",
            "kind": "Pod",
            "name": "web-0",
            "observed_at": naive(OBSERVED),
            "content_hash": "h",
            "body": {"kind": "Pod"},
            "lifecycle": "UPDATED",
        },
        "event_versions": {
            "namespace": "shop",
            "involved_kind": "Pod",
            "involved_name": "web-0",
            "dedup_key": "k",
            "event_at": naive(OBSERVED - timedelta(minutes=1)),
            "observed_at": naive(OBSERVED),
            "body": {"reason": "Unhealthy"},
        },
        "change_records": {
            "change_id": uuid_value(uuid4()),
            "timestamp": naive(CHANGED),
            "resource_type": "Deployment",
            "resource_name": "web",
            "change_type": "UPDATED",
            "scope": "DEPLOYMENT",
            "before": {},
            "after": {},
            "revision": "1",
            "source": "api",
        },
        "log_observations": {
            "incident_id": uuid_value(incident.incident_id),
            "service": "web",
            "event_at": naive(OBSERVED - timedelta(minutes=2)),
            "observed_at": naive(OBSERVED),
            "severity": "error",
            "message": "boom",
            "evidence_id": "loki:web:1:0",
            "dedup_key": "d",
            "source_system": "loki",
        },
    }
    with engine.begin() as connection:
        for name, values in rows.items():
            connection.execute(tables[name].insert().values(**values))


def _present(engine: Engine) -> set[tuple[str, str]]:
    inspector = inspect(engine)
    return {
        (table, column)
        for table, column in NEW_COLUMNS
        if column in {item["name"] for item in inspector.get_columns(table)}
    }


def _run(url: str) -> None:
    config = _config(url)
    command.upgrade(config, PREVIOUS)
    engine = create_engine(url)
    try:
        # Depending on which migrations rebuilt a table, a fresh database at
        # 0017 may already carry some of these columns (0001 builds tables from
        # live metadata). Drop them to model a database from before M19-3.2.
        with engine.begin() as connection:
            for table, column in sorted(_present(engine)):
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        assert _present(engine) == set()
        _seed_pre_m19_rows(engine)

        command.upgrade(config, TARGET)
        assert _present(engine) == set(NEW_COLUMNS)
        with Session(engine) as session:
            version = session.scalars(select(ObjectVersionRow)).one()
            assert version.ingested_at == version.observed_at
            assert version.source_at is None  # an observation time is not a source time
            event = session.scalars(select(EventVersionRow)).one()
            assert event.ingested_at == event.observed_at
            change = session.scalars(select(ChangeRecordRow)).one()
            assert change.ingested_at == change.timestamp == CHANGED
            log = session.scalars(select(LogObservationRow)).one()
            assert log.ingested_at == log.observed_at

        command.downgrade(config, PREVIOUS)
        assert _present(engine) == set()
        command.upgrade(config, TARGET)
        assert _present(engine) == set(NEW_COLUMNS)
    finally:
        engine.dispose()


def test_provenance_migration_backfills_and_reverses_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(f"sqlite:///{tmp_path / 'provenance.db'}")


@pytest.mark.postgres
def test_provenance_migration_backfills_and_reverses_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(postgres_url)


def test_new_rows_record_when_they_were_ingested(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest.db'}")
    Base.metadata.create_all(engine)
    before = datetime.now(UTC)
    incident = _incident()
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
        ObjectVersionRepository(session).record(
            {"kind": "Pod", "metadata": {"name": "web-0", "namespace": "shop"}}, OBSERVED
        )
        EventRepository(session).record(
            {
                "kind": "Event",
                "metadata": {"name": "e", "namespace": "shop", "uid": "e-1"},
                "involvedObject": {"kind": "Pod", "name": "web-0", "namespace": "shop"},
                "reason": "Unhealthy",
                "count": 1,
            },
            OBSERVED,
        )
        ChangeRecordRepository(session).append(
            ChangeRecord(
                timestamp=CHANGED,
                resource_type="Deployment",
                resource_name="web",
                change_type=ChangeType.UPDATED,
                before={},
                after={},
                revision="1",
                source="api",
            )
        )
        LogObservationRepository(session).record(
            incident.incident_id,
            [
                LogRecord(
                    service="web",
                    at=OBSERVED,
                    severity="error",
                    message="boom",
                    evidence_id="loki:web:1:0",
                )
            ],
            OBSERVED,
        )
    after = datetime.now(UTC)
    with Session(engine) as session:
        stamps = [
            session.scalars(select(model.ingested_at)).one()
            for model in (ObjectVersionRow, EventVersionRow, ChangeRecordRow, LogObservationRow)
        ]
        assert session.scalars(select(ObjectVersionRow.source_at)).one() is None
    assert all(stamp is not None and before <= stamp <= after for stamp in stamps)
    engine.dispose()
