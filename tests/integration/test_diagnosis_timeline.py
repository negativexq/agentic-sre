"""v2a: the diagnosis pipeline records a per-run timeline of phase events."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.main import create_app, diagnosis_phases
from packages.contracts import (
    Incident,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import IncidentEventRepository, IncidentRepository

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
_PHASES = {
    IncidentEventType.DIAGNOSIS_STARTED,
    IncidentEventType.EVIDENCE_GATHERED,
    IncidentEventType.HYPOTHESIS_CREATED,
    IncidentEventType.DIAGNOSIS_COMPLETED,
}


def _monotonic_clock() -> Any:
    """A clock that advances one second per call, for ordered timestamps."""
    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return T0 + timedelta(seconds=state["n"])

    return clock


def _app(tmp_path: Path) -> tuple[Any, Any, UUID, DiagnosisService]:
    engine = create_engine(f"sqlite:///{tmp_path / 'timeline.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Timeline test incident",
        created_at=T0,
        updated_at=T0,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), clock=_monotonic_clock()
    )
    return engine, factory, incident.incident_id, service


def _phase_events(factory: Any, incident_id: UUID) -> list[Any]:
    with Session(factory().bind) as session:
        events = IncidentEventRepository(session).list_for_incident(incident_id)
    return [event for event in events if event.event_type in _PHASES]


def test_one_diagnosis_run_emits_exactly_one_of_each_phase(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    counts = Counter(event.event_type for event in _phase_events(factory, incident_id))
    # G3: exactly one START and one COMPLETE (and one of each middle phase).
    assert counts[IncidentEventType.DIAGNOSIS_STARTED] == 1
    assert counts[IncidentEventType.DIAGNOSIS_COMPLETED] == 1
    assert counts[IncidentEventType.EVIDENCE_GATHERED] == 1
    assert counts[IncidentEventType.HYPOTHESIS_CREATED] == 1


def test_phase_timestamps_are_monotonic_within_a_run(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    events = _phase_events(factory, incident_id)
    # G4: the persisted sequence is non-decreasing in time.
    timestamps = [event.timestamp for event in events]
    assert timestamps == sorted(timestamps)


def test_evidence_gathered_reports_real_source_counts(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    gathered = next(
        event
        for event in _phase_events(factory, incident_id)
        if event.event_type is IncidentEventType.EVIDENCE_GATHERED
    )
    # G5: offline run has no reader, so every source count is a real zero.
    assert gathered.payload["objects"] == 0
    assert gathered.payload["journal"] == 0
    assert gathered.payload["events"] == 0
    assert gathered.payload["logs"] == 0
    assert "run_id" in gathered.payload


def test_rediagnosis_appends_a_new_run_rather_than_duplicating(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    service.run(incident_id)
    events = _phase_events(factory, incident_id)
    run_ids = {str(event.payload.get("run_id")) for event in events}
    # G6: two distinct runs, each complete.
    assert len(run_ids) == 2
    starts = [e for e in events if e.event_type is IncidentEventType.DIAGNOSIS_STARTED]
    completes = [e for e in events if e.event_type is IncidentEventType.DIAGNOSIS_COMPLETED]
    assert len(starts) == 2
    assert len(completes) == 2


def test_diagnosis_phases_selects_the_latest_run_in_order(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    service.run(incident_id)
    with Session(factory().bind) as session:
        events = IncidentEventRepository(session).list_for_incident(incident_id)
    phases = diagnosis_phases(events)
    assert [phase.name for phase in phases] == [
        "Diagnosis started",
        "Evidence gathered",
        "RCA engine completed",
        "Diagnosis stored",
    ]
    # The selected run is the most recent one.
    latest_complete = max(
        (e for e in events if e.event_type is IncidentEventType.DIAGNOSIS_COMPLETED),
        key=lambda e: e.timestamp,
    )
    assert phases[-1].at == latest_complete.timestamp


def test_events_api_returns_the_timeline_in_order(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        response = client.get(f"/api/v1/incidents/{incident_id}/events")
    assert response.status_code == 200
    body = response.json()
    phase_names = [
        item["event_type"] for item in body if item["event_type"] in {p.value for p in _PHASES}
    ]
    # G7: START first, COMPLETE last.
    assert phase_names[0] == IncidentEventType.DIAGNOSIS_STARTED.value
    assert phase_names[-1] == IncidentEventType.DIAGNOSIS_COMPLETED.value


def test_incident_page_shows_the_phase_timeline(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        page = client.get(f"/incidents/{incident_id}").text
    # G8: the rendered page carries the real per-phase timeline.
    assert "Diagnosis started" in page
    assert "Evidence gathered" in page
    assert "RCA engine completed" in page
    assert "Diagnosis stored" in page
    assert "T+" in page
