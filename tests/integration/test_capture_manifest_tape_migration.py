"""M19-3.3: snapshot cycle, manifest and provider tape schema, on SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.storage.models import (
    InvestigationReadRow,
    LogObservationRow,
    RunEvidenceManifestRow,
    SnapshotCycleObjectRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import IncidentRepository

PREVIOUS = "0018_temporal_provenance"
TARGET = "0019_capture_manifest_tape"
NEW_TABLES = (
    "investigation_reads",
    "run_evidence_manifest",
    "snapshot_cycle_objects",
    "snapshot_cycles",
)
MODELS = {
    "snapshot_cycles": SnapshotCycleRow,
    "snapshot_cycle_objects": SnapshotCycleObjectRow,
    "run_evidence_manifest": RunEvidenceManifestRow,
    "investigation_reads": InvestigationReadRow,
    "log_observations": LogObservationRow,
}
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _read(sequence: int, **overrides: Any) -> InvestigationReadRow:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "sequence": sequence,
        "caller_class": "ENGINE",
        "capability": "resource_pressure",
        "query_key": "q-1",
        "query_descriptor": {"pod": "shop/Pod/web-0"},
        "started_at": AT,
        "finished_at": AT,
        "committed_at": AT,
        "status": "SUCCESS",
        "observation": {"series": []},
        "evidence_ids": [],
    }
    return InvestigationReadRow(**(values | overrides))


def _rejected(engine: Engine, build: Callable[[], Any]) -> bool:
    with Session(engine) as session:
        session.add(build())
        try:
            session.commit()
        except IntegrityError:
            return True
    return False


def _has_source_read_id(engine: Engine) -> bool:
    return "source_read_id" in {
        column["name"] for column in inspect(engine).get_columns("log_observations")
    }


def _assert_schema_and_constraints(engine: Engine) -> None:
    inspector = inspect(engine)
    for table, model in MODELS.items():
        assert {c["name"] for c in inspector.get_columns(table)} == set(
            model.__table__.columns.keys()
        )
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("investigation_reads")
    } == {("run_id", "sequence")}
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("run_evidence_manifest")
    } == {("run_id", "source_type", "source_id"), ("run_id", "sequence")}
    assert any(
        fk["referred_table"] == "investigation_reads"
        and fk["constrained_columns"] == ["source_read_id"]
        for fk in inspector.get_foreign_keys("log_observations")
    )

    body = {
        "kind": "Pod",
        "metadata": {"name": "web-0", "namespace": "shop", "uid": "uid-a"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}], "phase": "Running"},
    }
    with Session(engine) as session:
        cycle = SnapshotCycleRow(
            run_id="run-1",
            started_at=AT,
            observed_at=AT,
            completed_at=AT,
            completed_scopes=[["shop", "Pod"]],
            failed_scopes=[],
        )
        session.add(cycle)
        session.flush()
        session.add(
            SnapshotCycleObjectRow(
                cycle_id=cycle.cycle_id,
                object_key="shop/Pod/web-0",
                namespace="shop",
                kind="Pod",
                name="web-0",
                uid="uid-a",
                body=body,
                evidence_id=f"snapshot:{cycle.cycle_id}:shop/Pod/web-0",
            )
        )
        # The same canonical query may be read again in one run.
        session.add_all([_read(1), _read(2)])
        session.add(
            RunEvidenceManifestRow(
                run_id="run-1", sequence=1, source_type="SNAPSHOT_CYCLE", source_id="1"
            )
        )
        session.commit()
        cycle_id = cycle.cycle_id
        read_id = session.scalars(select(InvestigationReadRow.read_id)).first()
    with Session(engine) as session:
        stored = session.scalars(select(SnapshotCycleObjectRow.body)).one()
    assert stored == body  # full body, status included

    assert _rejected(engine, lambda: _read(1))  # duplicate (run_id, sequence)
    assert _rejected(engine, lambda: _read(9, caller_class="PLANNER"))
    assert _rejected(engine, lambda: _read(9, status="PARTIAL"))
    assert not _rejected(engine, lambda: _read(1, run_id="run-2"))
    assert _rejected(
        engine,
        lambda: RunEvidenceManifestRow(
            run_id="run-1", sequence=2, source_type="SNAPSHOT_CYCLE", source_id="1"
        ),
    )
    assert _rejected(
        engine,
        lambda: RunEvidenceManifestRow(
            run_id="run-1", sequence=1, source_type="LOG", source_id="7"
        ),
    )
    assert _rejected(
        engine,
        lambda: SnapshotCycleObjectRow(
            cycle_id=cycle_id,
            object_key="shop/Pod/web-1",
            namespace="shop",
            kind="Pod",
            name="web-1",
            body={},
            evidence_id=f"snapshot:{cycle_id}:shop/Pod/web-0",
        ),
    )

    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="latency",
        created_at=AT,
        updated_at=AT,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
        session.add(
            LogObservationRow(
                incident_id=incident.incident_id,
                service="web",
                observed_at=AT,
                severity="error",
                message="boom",
                evidence_id="loki:web:1:0",
                dedup_key="d",
                source_read_id=read_id,
            )
        )
        session.commit()
        assert session.scalars(select(LogObservationRow.source_read_id)).one() == read_id


def _run(url: str) -> None:
    config = _config(url)
    command.upgrade(config, PREVIOUS)
    engine = create_engine(url)
    try:
        # Revision 0001 builds tables from live metadata; drop the new column and
        # tables to model a database that reached 0018 before M19-3.3.
        with engine.begin() as connection:
            if _has_source_read_id(engine):
                operations = Operations(MigrationContext.configure(connection))
                with operations.batch_alter_table("log_observations") as batch:
                    batch.drop_constraint("fk_log_observations_source_read_id", type_="foreignkey")
                    batch.drop_column("source_read_id")
            existing = set(inspect(connection).get_table_names())
            for name in NEW_TABLES:
                if name in existing:
                    connection.execute(text(f"DROP TABLE {name}"))
        assert not _has_source_read_id(engine)
        assert not set(NEW_TABLES) & set(inspect(engine).get_table_names())

        command.upgrade(config, TARGET)
        _assert_schema_and_constraints(engine)

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM log_observations"))
        command.downgrade(config, PREVIOUS)
        assert not set(NEW_TABLES) & set(inspect(engine).get_table_names())
        assert not _has_source_read_id(engine)

        command.upgrade(config, TARGET)
        assert set(NEW_TABLES) <= set(inspect(engine).get_table_names())
        assert _has_source_read_id(engine)
    finally:
        engine.dispose()


def test_capture_manifest_tape_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(f"sqlite:///{tmp_path / 'tape.db'}")


@pytest.mark.postgres
def test_capture_manifest_tape_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(postgres_url)
