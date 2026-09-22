"""v2a: the diagnosis pipeline records a per-run timeline of phase events."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.main import create_app, diagnosis_phases, newer_run_note
from packages.contracts import (
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import (
    DiagnosisRepository,
    IncidentEventRepository,
    IncidentRepository,
)

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
_PHASES = {
    IncidentEventType.DIAGNOSIS_STARTED,
    IncidentEventType.EVIDENCE_GATHERED,
    IncidentEventType.RCA_ENGINE_COMPLETED,
    IncidentEventType.DIAGNOSIS_COMPLETED,
}


def _event(event_type: IncidentEventType, run_id: str, seconds: int) -> IncidentEvent:
    return IncidentEvent(
        incident_id=uuid4(),
        event_type=event_type,
        timestamp=T0 + timedelta(seconds=seconds),
        correlation_id=uuid4(),
        payload={"run_id": run_id},
    )


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
    assert counts[IncidentEventType.RCA_ENGINE_COMPLETED] == 1


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


def test_stored_diagnosis_carries_its_run_id_and_binds_the_timeline(tmp_path: Path) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    service.run(incident_id)
    with Session(factory().bind) as session:
        repository = DiagnosisRepository(session)
        document = repository.latest(incident_id)
        run_id = repository.latest_run_id(incident_id)
        events = IncidentEventRepository(session).list_for_incident(incident_id)
    assert document is not None
    # The pure document is unchanged; the run id lives in its own column.
    assert "diagnosis_run_id" not in document
    assert run_id
    phases = diagnosis_phases(events, run_id)
    assert [phase.name for phase in phases] == [
        "Diagnosis started",
        "Evidence gathered",
        "RCA engine completed",
        "Diagnosis stored",
    ]
    # Every phase belongs to the run that produced the stored diagnosis.
    run_events = {e.timestamp for e in events if str(e.payload.get("run_id")) == run_id}
    assert all(phase.at in run_events for phase in phases)


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


def test_phases_ignore_a_newer_incomplete_run() -> None:
    """The mismatch fix: an old completed run plus a newer half-run must render
    the completed run's timeline, never the incomplete one."""
    ES = IncidentEventType
    events = [
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.EVIDENCE_GATHERED, "run-1", 1),
        _event(ES.RCA_ENGINE_COMPLETED, "run-1", 2),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
        # A newer run that crashed after starting.
        _event(ES.DIAGNOSIS_STARTED, "run-2", 10),
        _event(ES.EVIDENCE_GATHERED, "run-2", 11),
    ]
    phases = diagnosis_phases(events, "run-1")
    assert [p.name for p in phases] == [
        "Diagnosis started",
        "Evidence gathered",
        "RCA engine completed",
        "Diagnosis stored",
    ]
    # Only run-1's phases render; the newer half-run run-2 is never mixed in.
    assert phases[0].at == T0
    assert phases[-1].at == T0 + timedelta(seconds=3)


def test_newer_run_note_flags_a_failed_later_run() -> None:
    ES = IncidentEventType
    events = [
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
        _event(ES.DIAGNOSIS_STARTED, "run-2", 10),
        _event(ES.DIAGNOSIS_FAILED, "run-2", 11),
    ]
    phases = diagnosis_phases(events, "run-1")
    note = newer_run_note(events, phases)
    assert note is not None
    assert "failed" in note


def test_newer_run_note_flags_a_still_running_later_run() -> None:
    ES = IncidentEventType
    events = [
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
        _event(ES.DIAGNOSIS_STARTED, "run-2", 10),
        _event(ES.EVIDENCE_GATHERED, "run-2", 11),
    ]
    note = newer_run_note(events, diagnosis_phases(events, "run-1"))
    assert note is not None
    assert "in progress" in note


def test_no_note_when_the_latest_run_completed() -> None:
    ES = IncidentEventType
    events = [
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
    ]
    assert newer_run_note(events, diagnosis_phases(events, "run-1")) is None


def test_a_failed_run_emits_diagnosis_failed_and_reraises(tmp_path: Path, monkeypatch: Any) -> None:
    _engine, factory, incident_id, service = _app(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("apps.control_plane.diagnosis.diagnose", boom)
    try:
        service.run(incident_id)
    except RuntimeError:
        pass
    else:
        raise AssertionError("run() should have re-raised the engine error")

    with Session(factory().bind) as session:
        events = IncidentEventRepository(session).list_for_incident(incident_id)
    kinds = {event.event_type for event in events}
    assert IncidentEventType.DIAGNOSIS_STARTED in kinds
    assert IncidentEventType.DIAGNOSIS_FAILED in kinds
    assert IncidentEventType.DIAGNOSIS_COMPLETED not in kinds


def test_lost_completing_event_yields_no_phases_for_that_run() -> None:
    """run_id binding: if the run's DIAGNOSIS_COMPLETED was lost, its timeline is
    empty (the page then says 'unavailable') rather than borrowing another run."""
    ES = IncidentEventType
    events = [
        # An older run that completed cleanly.
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
        # The shown diagnosis is run-2, whose completing event never persisted.
        _event(ES.DIAGNOSIS_STARTED, "run-2", 10),
        _event(ES.EVIDENCE_GATHERED, "run-2", 11),
        _event(ES.RCA_ENGINE_COMPLETED, "run-2", 12),
    ]
    assert diagnosis_phases(events, "run-2") == ()
    # And it never falls back to run-1's timeline.
    assert diagnosis_phases(events, "run-1")[0].at == T0


def test_no_run_id_yields_no_phases() -> None:
    events = [_event(IncidentEventType.DIAGNOSIS_STARTED, "run-1", 0)]
    assert diagnosis_phases(events, None) == ()


def test_newer_run_note_picks_the_newest_incomplete_run() -> None:
    ES = IncidentEventType
    events = [
        _event(ES.DIAGNOSIS_STARTED, "run-1", 0),
        _event(ES.DIAGNOSIS_COMPLETED, "run-1", 3),
        # An in-progress run and, after it, a failed one: the newest wins.
        _event(ES.DIAGNOSIS_STARTED, "run-2", 10),
        _event(ES.EVIDENCE_GATHERED, "run-2", 11),
        _event(ES.DIAGNOSIS_STARTED, "run-3", 20),
        _event(ES.DIAGNOSIS_FAILED, "run-3", 21),
    ]
    note = newer_run_note(events, diagnosis_phases(events, "run-1"))
    assert note is not None
    assert "failed" in note


def test_incident_page_reports_an_unavailable_timeline(tmp_path: Path) -> None:
    from sqlalchemy import text

    _engine, factory, incident_id, service = _app(tmp_path)
    service.run(incident_id)
    # Drop this run's pipeline timeline events, keeping the stored diagnosis.
    with Session(factory().bind) as session:
        session.execute(
            text(
                "delete from incident_events where event_type in "
                "('DIAGNOSIS_STARTED','EVIDENCE_GATHERED','RCA_ENGINE_COMPLETED',"
                "'DIAGNOSIS_COMPLETED')"
            )
        )
        session.commit()
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        page = client.get(f"/incidents/{incident_id}").text
    assert "Timeline unavailable or incomplete" in page
