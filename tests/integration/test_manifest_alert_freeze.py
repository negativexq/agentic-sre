"""M19-3.6a: a run's alerts are the content its manifest froze, not the mutable row."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService, _rca_alert_from_payload
from packages.rca.engine import diagnose
from packages.rca.live import LiveSource
from packages.rca.manifest import ManifestEntry, event_evidence_id
from packages.storage import manifest as manifest_module
from packages.storage.manifest import ManifestAlertPayloadMissing, load_manifest, load_members
from packages.storage.models import AlertRow, RunEvidenceManifestRow
from packages.storage.repositories import AlertRepository, DiagnosisRepository

PAYLOAD_KEYS = {
    "alert_name",
    "service",
    "namespace",
    "starts_at",
    "ends_at",
    "status",
    "labels",
    "fingerprint",
}


def _run(world: Any) -> tuple[sessionmaker[Session], UUID, str]:
    factory, cluster, clock, incident_id = world
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id)
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, incident_id, run_id


def _replayed(factory: sessionmaker[Session], run_id: str) -> tuple[Any, Any]:
    """Rebuild the run's RCA input from its manifest alone and diagnose it."""
    with factory() as session:
        members = load_members(session, load_manifest(session, run_id))
    source = LiveSource(
        incident="replay",
        alert_items=[_rca_alert_from_payload(item) for item in members.alerts],
        journal=list(members.journal),
        current_objects=list(members.snapshot.objects) if members.snapshot else [],
        event_bodies=[body for _, body in members.events],
        event_evidence_ids=[event_evidence_id(version_id) for version_id, _ in members.events],
        error_items=list(members.logs),
        observed_at=T0 + timedelta(minutes=30),
        lifecycle_records=members.lifecycle,
        snapshot_cycle_id=members.snapshot.cycle_id if members.snapshot else None,
        snapshot_observed_at=members.snapshot.observed_at if members.snapshot else None,
    )
    return members.alerts, diagnose(source)


def test_manifest_alert_rows_carry_their_frozen_content(
    setup: Any,  # noqa: F811
) -> None:
    factory, _, run_id = _run(setup)
    with factory() as session:
        rows = session.scalars(
            select(RunEvidenceManifestRow).where(RunEvidenceManifestRow.run_id == run_id)
        ).all()
    alerts = [row for row in rows if row.source_type == "ALERT"]
    assert alerts
    assert all(row.payload is not None and set(row.payload) == PAYLOAD_KEYS for row in alerts)
    assert all(row.payload is None for row in rows if row.source_type != "ALERT")
    assert alerts[0].payload is not None and alerts[0].payload["status"] == "FIRING"


def test_resolving_the_alert_after_the_manifest_changes_nothing_the_run_knows(
    setup: Any,  # noqa: F811
) -> None:
    factory, incident_id, run_id = _run(setup)
    before_alerts, before = _replayed(factory, run_id)

    # Alert ingestion updates the row in place when the alert resolves.
    with factory() as session:
        row = session.scalars(select(AlertRow).where(AlertRow.incident_id == incident_id)).one()
        row.status = "RESOLVED"
        row.ends_at = T0 + timedelta(minutes=45)
        row.labels = {**row.labels, "late": "label"}
        session.commit()

    after_alerts, after = _replayed(factory, run_id)
    assert after_alerts == before_alerts
    for key in PAYLOAD_KEYS:  # every frozen field, identity fields included
        assert after_alerts[0][key] == before_alerts[0][key], key
    assert [_rca_alert_from_payload(item) for item in after_alerts] == [
        _rca_alert_from_payload(item) for item in before_alerts
    ]
    assert after_alerts[0]["status"] == "FIRING" and after_alerts[0]["ends_at"] is None
    assert "late" not in after_alerts[0]["labels"]
    assert (after.resolution, after.root_cause) == (before.resolution, before.root_cause)
    assert [h.hypothesis_id for h in after.alternatives] == [
        h.hypothesis_id for h in before.alternatives
    ]


def test_an_alert_entry_without_frozen_content_is_an_explicit_error(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, _ = _run(setup)
    with factory() as session:
        alert_id = session.scalars(
            select(AlertRow.alert_id).where(AlertRow.incident_id == incident_id)
        ).one()

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("the current alert row must not be read")

    # Any read of the mutable alert row would fail loudly.
    monkeypatch.setattr(AlertRepository, "ids_for_incident", never)
    monkeypatch.setattr(AlertRepository, "list_for_incident", never)
    monkeypatch.setattr(manifest_module, "alert_payload", never)
    real_get = Session.get

    def guarded_get(self: Session, entity: Any, *args: Any, **kwargs: Any) -> Any:
        if entity is AlertRow:
            never()
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", guarded_get)
    with factory() as session, pytest.raises(ManifestAlertPayloadMissing):
        load_members(session, [ManifestEntry("ALERT", str(alert_id))])


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _has_payload(url: str) -> bool:
    engine = create_engine(url)
    try:
        return "payload" in {
            column["name"] for column in inspect(engine).get_columns("run_evidence_manifest")
        }
    finally:
        engine.dispose()


def _migrate(url: str) -> None:
    config = _config(url)
    command.upgrade(config, "0019_capture_manifest_tape")
    if _has_payload(url):  # 0001 builds tables from live metadata
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE run_evidence_manifest DROP COLUMN payload")
        engine.dispose()
    assert not _has_payload(url)
    command.upgrade(config, "0020_manifest_payload")
    assert _has_payload(url)
    command.downgrade(config, "0019_capture_manifest_tape")
    assert not _has_payload(url)
    command.upgrade(config, "0020_manifest_payload")
    assert _has_payload(url)


def test_manifest_payload_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _migrate(f"sqlite:///{tmp_path / 'payload.db'}")


@pytest.mark.postgres
def test_manifest_payload_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _migrate(postgres_url)
