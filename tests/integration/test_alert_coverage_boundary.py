"""M21 amendment 4 P2: the run boundary freezes alert-channel coverage; replay reads it exactly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

import apps.control_plane.diagnosis as diagnosis_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.alert_coverage import (
    ALERT_COVERAGE_SOURCE,
    COVERAGE_CONTIGUOUS,
    COVERAGE_UNAVAILABLE,
    AlertCoverageBoundary,
    AlertCoverageConfig,
)
from packages.rca.engine import diagnose as engine_diagnose
from packages.rca.investigation.environment import InitialObservationView
from packages.rca.investigation.evidence import InMemoryEvidenceStore, OverlayObservationSource
from packages.rca.replay import ReplaySource
from packages.rca.source import InMemorySource
from packages.storage.database import create_session_factory
from packages.storage.manifest import ReplayDataError, load_run_boundary
from packages.storage.models import AlertCoverageSegmentRow, Base, IncidentEventRow
from packages.storage.repositories import AlertCoverageRepository, DiagnosisRepository

CONFIG = AlertCoverageConfig(poll_interval=timedelta(seconds=60), max_gap_polls=2)
W0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _poll(factory: sessionmaker[Session], at: datetime, *, ok: bool = True) -> None:
    with factory() as session:
        repository = AlertCoverageRepository(session)
        if ok:
            repository.record_success(
                source=ALERT_COVERAGE_SOURCE,
                attempted_at=at,
                completed_at=at,
                active_alerts=1,
                config=CONFIG,
            )
        else:
            repository.record_failure(
                source=ALERT_COVERAGE_SOURCE, attempted_at=at, completed_at=at, error_type="E"
            )


def _boundary(factory: sessionmaker[Session], at: datetime) -> AlertCoverageBoundary:
    with factory() as session:
        return AlertCoverageRepository(session).boundary_at(
            source=ALERT_COVERAGE_SOURCE, at=at, config=CONFIG
        )


def _minutes(value: float) -> datetime:
    return W0 + timedelta(minutes=value)


def test_the_boundary_lies_in_the_latest_contiguous_segment(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    assert _boundary(factory, W0).status == COVERAGE_UNAVAILABLE  # never polled
    for minute in (0, 1, 2):
        _poll(factory, _minutes(minute))
    _poll(factory, _minutes(2.5), ok=False)  # 12:02:30 explicit failure: segment ends 12:02
    for minute in (20, 21, 22):
        _poll(factory, _minutes(minute))
    # Inside the first, closed segment: covered since its own start.
    inside = _boundary(factory, _minutes(1.5))
    assert (inside.status, inside.alert_observation_start) == (COVERAGE_CONTIGUOUS, _minutes(0))
    # In the failure gap: unknown, whatever came before or after.
    assert _boundary(factory, _minutes(10)).status == COVERAGE_UNAVAILABLE
    # The latest open segment, a heartbeat or two after its last success: covered.
    live = _boundary(factory, _minutes(23.9))
    assert (live.status, live.alert_observation_start) == (COVERAGE_CONTIGUOUS, _minutes(20))
    # The poller went silent: the open segment no longer vouches for this moment.
    assert _boundary(factory, _minutes(40)).status == COVERAGE_UNAVAILABLE


def _run(world: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[datetime | None]]:
    factory, cluster, clock, incident_id = world
    seen: list[datetime | None] = []
    real = engine_diagnose

    def recording(source: Any, **kwargs: Any) -> Any:
        seen.append(source.alert_observation_start())
        return real(source, **kwargs)

    monkeypatch.setattr(diagnosis_module, "diagnose", recording)
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        alert_coverage_config=CONFIG,
    )
    service.snapshot()
    clock.now = T0 + timedelta(minutes=13)
    service.run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return run_id, seen


def test_a_live_run_freezes_w_and_replay_reads_it_exactly(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, *_ = setup
    for minute in range(0, 14):
        _poll(factory, T0 + timedelta(minutes=minute))
    run_id, seen = _run(setup, monkeypatch)

    with factory() as session:
        frozen = load_run_boundary(session, run_id).alert_coverage
    assert frozen is not None
    assert frozen.status == COVERAGE_CONTIGUOUS
    assert frozen.alert_observation_start == T0
    # The live engine saw exactly the persisted W.
    assert seen == [T0]
    # Replay reads the boundary, never the segments: rewriting them changes nothing.
    with factory() as session:
        session.execute(delete(AlertCoverageSegmentRow))
        session.commit()
    replay = ReplaySource.from_run(run_id, session_factory=factory)
    assert replay.alert_observation_start() == T0


def test_without_coverage_w_is_unknown_live_and_in_replay(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, *_ = setup
    run_id, seen = _run(setup, monkeypatch)
    with factory() as session:
        frozen = load_run_boundary(session, run_id).alert_coverage
    assert frozen is not None and frozen.status == COVERAGE_UNAVAILABLE
    assert seen == [None]
    assert ReplaySource.from_run(run_id, session_factory=factory).alert_observation_start() is None


def _payload(factory: sessionmaker[Session], run_id: str) -> tuple[UUID, dict[str, Any]]:
    with factory() as session:
        row = next(
            row
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
            if row.payload.get("run_id") == run_id
        )
        return row.event_id, dict(row.payload)


def _rewrite(factory: sessionmaker[Session], event_id: UUID, payload: dict[str, Any]) -> None:
    with factory() as session:
        session.execute(
            update(IncidentEventRow)
            .where(IncidentEventRow.event_id == event_id)
            .values(payload=payload)
        )
        session.commit()


def test_a_legacy_boundary_has_no_coverage_and_a_malformed_one_is_refused(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, *_ = setup
    run_id, _ = _run(setup, monkeypatch)
    event_id, payload = _payload(factory, run_id)

    legacy = {key: value for key, value in payload.items() if key != "alert_coverage"}
    _rewrite(factory, event_id, legacy)
    with factory() as session:
        assert load_run_boundary(session, run_id).alert_coverage is None

    for broken in (
        {
            "status": "CONTIGUOUS",
            "segment_id": None,
            "observation_start": None,
            "last_success": None,
        },
        {"status": "MAYBE", "segment_id": None, "observation_start": None, "last_success": None},
        {"status": "UNAVAILABLE", "segment_id": None, "observation_start": None},
        {**payload["alert_coverage"], "observation_start": "not-a-time"},
    ):
        _rewrite(factory, event_id, {**payload, "alert_coverage": broken})
        with factory() as session, pytest.raises(ReplayDataError):
            load_run_boundary(session, run_id)


def test_views_and_overlays_inherit_the_base_boundary() -> None:
    base = InMemorySource(name="i", alert_coverage_start=W0)
    overlay = OverlayObservationSource(
        base=base,
        store=InMemoryEvidenceStore(),
        acquired_evidence_refs=(),
        supported_capabilities=frozenset(),
    )
    assert overlay.alert_observation_start() == W0
    assert InitialObservationView(full_source=base).alert_observation_start() == W0
