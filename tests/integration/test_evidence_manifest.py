"""M19-3.6: capture -> manifest (with its boundary event) -> RCA from manifest members only."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, FakeLogs, setup  # noqa: F401 - pytest fixture

import apps.control_plane.diagnosis as diagnosis_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.contracts import (
    ChangeRecord,
    ChangeType,
    Incident,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.live import LiveSource
from packages.rca.manifest import SOURCE_TYPES
from packages.rca.model import LogRecord
from packages.rca.provider_adapter import ProviderReaders
from packages.storage.database import create_session_factory
from packages.storage.evidence_guard import AuthoritativeEvidenceMutation
from packages.storage.manifest import ManifestRequest, build_manifest, load_manifest
from packages.storage.models import (
    AlertRow,
    Base,
    ChangeRecordRow,
    EventVersionRow,
    IncidentEventRow,
    InvestigationReadRow,
    InvestigationRunRow,
    LifecycleObservationRow,
    LogObservationRow,
    ObjectVersionRow,
    RunEvidenceManifestRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import (
    AlertRepository,
    ChangeRecordRepository,
    DiagnosisRepository,
    IncidentEventRepository,
    IncidentRepository,
)

POD = {
    "kind": "Pod",
    "metadata": {"name": "payment-service-7d9f-a1", "namespace": "sre-demo", "uid": "pod-uid-1"},
    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
}
WARNING = {
    "kind": "Event",
    "metadata": {"name": "payment.backoff", "namespace": "sre-demo", "uid": "k8s-event-uid"},
    "involvedObject": {
        "kind": "Pod",
        "name": "payment-service-7d9f-a1",
        "namespace": "sre-demo",
        "uid": "pod-uid-1",
    },
    "reason": "BackOff",
    "type": "Warning",
    "message": "back-off restarting failed container",
    "firstTimestamp": (T0 + timedelta(minutes=9)).isoformat(),
    "lastTimestamp": (T0 + timedelta(minutes=9)).isoformat(),
    "count": 1,
}


class Seen:
    """What the diagnosis's LiveSource exposed to RCA."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sources: list[LiveSource] = []
        original = LiveSource.object_history

        def spy(source: LiveSource) -> Any:
            self.sources.append(source)
            return original(source)

        monkeypatch.setattr(LiveSource, "object_history", spy)

    @property
    def source(self) -> LiveSource:
        assert self.sources
        return self.sources[0]


def _run(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    before_manifest: Callable[[], None] | None = None,
) -> tuple[sessionmaker[Session], UUID, str, Seen]:
    factory, cluster, clock, incident_id = world
    cluster.objects.append(dict(POD))
    cluster.events = [dict(WARNING)]
    with factory() as session:
        ChangeRecordRepository(session).append(
            ChangeRecord(
                timestamp=T0 + timedelta(minutes=5),
                resource_type="Deployment",
                resource_name="payment-service",
                change_type=ChangeType.UPDATED,
                before={},
                after={},
                revision="2",
                source="api",
            )
        )
    logs = FakeLogs(
        [
            LogRecord(
                service="payment-service",
                at=T0 + timedelta(minutes=10),
                severity="error",
                message="timeout calling bank",
                evidence_id="loki:payment-service:1:0",
            )
        ]
    )
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=ProviderReaders(loki=logs),
        clock=clock,
    )
    seen = Seen(monkeypatch)
    if before_manifest is not None:
        real = build_manifest

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            before_manifest()
            return real(*args, **kwargs)

        monkeypatch.setattr(diagnosis_module, "build_manifest", wrapped)
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id)
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, incident_id, run_id, seen


def _boundary_events(factory: sessionmaker[Session], incident_id: UUID) -> list[dict[str, Any]]:
    with factory() as session:
        return [
            event.payload
            for event in IncidentEventRepository(session).list_for_incident(incident_id)
            if event.event_type is IncidentEventType.EVIDENCE_GATHERED
        ]


def test_capture_is_committed_before_the_manifest_is_taken(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = setup[0]
    counts: dict[str, int] = {}

    def observe() -> None:
        with factory() as session:  # a separate session sees only committed rows
            counts["cycles"] = session.scalar(select(func.count()).select_from(SnapshotCycleRow))
            counts["logs"] = session.scalar(select(func.count()).select_from(LogObservationRow))
            counts["events"] = session.scalar(select(func.count()).select_from(EventVersionRow))

    _run(setup, monkeypatch, before_manifest=observe)
    assert counts == {"cycles": 1, "logs": 1, "events": 1}


def test_one_boundary_event_carries_the_single_window_end(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, run_id, seen = _run(setup, monkeypatch)

    (payload,) = [p for p in _boundary_events(factory, incident_id) if p["run_id"] == run_id]
    window_end = datetime.fromisoformat(payload["window_end"])
    assert "manifest_entries" not in payload
    assert window_end == seen.source.observed_at  # the one boundary RCA uses
    with factory() as session:
        cycle = session.scalars(select(SnapshotCycleRow)).one()
        captured = session.scalars(select(LogObservationRow.observed_at)).all()
    assert payload["snapshot_cycle_id"] == cycle.cycle_id
    # The boundary is taken once the whole capture has finished.
    assert cycle.completed_at <= window_end
    assert all(at <= window_end for at in captured)


def test_manifest_rows_are_exact_persistent_ids(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, run_id, _ = _run(setup, monkeypatch)
    tables: dict[str, tuple[Any, Callable[[str], Any]]] = {
        "SNAPSHOT_CYCLE": (SnapshotCycleRow, int),
        "ALERT": (AlertRow, UUID),
        "OBJECT_VERSION": (ObjectVersionRow, int),
        "EVENT_VERSION": (EventVersionRow, int),
        "LIFECYCLE": (LifecycleObservationRow, int),
        "CHANGE": (ChangeRecordRow, UUID),
        "LOG": (LogObservationRow, int),
    }
    with factory() as session:
        entries = load_manifest(session, run_id)
        sequences = session.scalars(
            select(RunEvidenceManifestRow.sequence)
            .where(RunEvidenceManifestRow.run_id == run_id)
            .order_by(RunEvidenceManifestRow.sequence)
        ).all()
        assert sequences == list(range(1, len(entries) + 1))
        for entry in entries:
            model, parse = tables[entry.source_type]
            assert session.get(model, parse(entry.source_id)) is not None, entry
    types = [entry.source_type for entry in entries]
    assert set(types) == set(SOURCE_TYPES)  # every source class is represented
    assert types == sorted(types, key=SOURCE_TYPES.index)  # canonical order


def test_rca_sees_only_manifest_members(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, run_id, seen = _run(setup, monkeypatch)
    source = seen.source
    with factory() as session:
        entries = load_manifest(session, run_id)
        ids = {
            kind: {e.source_id for e in entries if e.source_type == kind} for kind in SOURCE_TYPES
        }
        log_ids = set(
            session.scalars(
                select(LogObservationRow.evidence_id).where(
                    LogObservationRow.observation_id.in_([int(i) for i in ids["LOG"]])
                )
            )
        )
        lifecycle_ids = set(
            session.scalars(
                select(LifecycleObservationRow.evidence_id).where(
                    LifecycleObservationRow.observation_id.in_([int(i) for i in ids["LIFECYCLE"]])
                )
            )
        )
    (cycle,) = ids["SNAPSHOT_CYCLE"]
    history_ids = {v.evidence_id for vs in source.object_history().values() for v in vs}
    assert history_ids <= {f"journal:{i}" for i in ids["OBJECT_VERSION"]} | {
        item for item in history_ids if item.startswith(f"snapshot:{cycle}:")
    }
    # Event evidence ids are the persisted event_versions rows, not the Kubernetes UID.
    event_ids = {event.evidence_id for event in source.events()}
    assert event_ids and event_ids == {f"event:{i}" for i in ids["EVENT_VERSION"]}
    assert "event:k8s-event-uid" not in event_ids
    assert {record.evidence_id for record in source.error_logs()} == log_ids
    assert {s.evidence_id for s in source.pod_status_observations()} <= lifecycle_ids
    assert {alert.name for alert in source.alerts()} == {"HighLatency"}
    # change_records are manifest members for provenance only: RCA has no consumer.
    assert ids["CHANGE"]
    assert not any(e.startswith("change") for e in history_ids | event_ids)


def test_a_default_deterministic_run_persists_boundary_and_capture_tape(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, run_id, _ = _run(setup, monkeypatch)
    (payload,) = [p for p in _boundary_events(factory, incident_id) if p["run_id"] == run_id]
    assert datetime.fromisoformat(payload["window_end"]).tzinfo is not None
    with factory() as session:
        # No bounded investigation ran, so no investigation artifact. CAPTURE
        # provider reads are durably taped before their results are consumed.
        assert session.scalar(select(func.count()).select_from(InvestigationRunRow)) == 0
        capture_reads = list(
            session.scalars(
                select(InvestigationReadRow).where(
                    InvestigationReadRow.run_id == run_id,
                    InvestigationReadRow.caller_class == "CAPTURE",
                )
            )
        )
        assert capture_reads
        assert all(row.status == "SUCCESS" for row in capture_reads)
        assert all(row.run_id == run_id for row in capture_reads)
        read_ids = {row.read_id for row in capture_reads}
        new_logs = list(
            session.scalars(
                select(LogObservationRow).where(
                    LogObservationRow.evidence_id == "loki:payment-service:1:0"
                )
            )
        )
        assert new_logs
        assert all(log.source_read_id in read_ids for log in new_logs)


def test_manifest_rows_are_append_only(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, run_id, _ = _run(setup, monkeypatch)
    with factory() as session:
        row = session.scalars(
            select(RunEvidenceManifestRow).where(RunEvidenceManifestRow.run_id == run_id)
        ).first()
        assert row is not None
        row.source_id = "tampered"
        with pytest.raises(AuthoritativeEvidenceMutation):
            session.flush()


# --- the manifest transaction on its own ---------------------------------------


@contextmanager
def _world(url: str) -> Iterator[tuple[sessionmaker[Session], Incident]]:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="latency",
        created_at=T0,
        updated_at=T0,
    )
    with factory() as session:
        IncidentRepository(session).create(incident)
        session.add(
            EventVersionRow(
                namespace="sre-demo",
                involved_kind="Pod",
                involved_name="p",
                dedup_key="first",
                event_at=T0 + timedelta(minutes=1),
                observed_at=T0 + timedelta(minutes=1),
                body={"metadata": {"uid": "first"}, "reason": "BackOff"},
            )
        )
        session.commit()
    try:
        yield factory, incident
    finally:
        engine.dispose()


def _request(incident: Incident, run_id: str = "run-1") -> ManifestRequest:
    return ManifestRequest(
        run_id=run_id,
        incident_id=incident.incident_id,
        correlation_id=incident.correlation_id,
        starts_at=T0 - timedelta(hours=2),
        ends_at=T0 + timedelta(minutes=30),
        window_end=T0 + timedelta(minutes=30),
        namespaces=frozenset({"sre-demo"}),
        journal_namespaces=frozenset({"sre-demo"}),
        snapshot_cycle_id=None,
        listed_objects=0,
    )


def _counts(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as session:
        manifest = session.scalar(select(func.count()).select_from(RunEvidenceManifestRow))
        boundary = session.scalar(
            select(func.count())
            .select_from(IncidentEventRow)
            .where(IncidentEventRow.event_type == IncidentEventType.EVIDENCE_GATHERED.value)
        )
    return manifest or 0, boundary or 0


def test_a_failed_boundary_event_leaves_no_manifest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world(f"sqlite:///{tmp_path / 'm1.db'}") as (factory, incident):

        def fail(*_: Any, **__: Any) -> Any:
            raise RuntimeError("injected boundary event failure")

        monkeypatch.setattr(IncidentEventRepository, "append", fail)
        with pytest.raises(RuntimeError):
            build_manifest(factory, _request(incident), timestamp=T0)
        assert _counts(factory) == (0, 0)


def test_a_failed_manifest_insert_leaves_no_boundary_event(tmp_path: Any) -> None:
    with _world(f"sqlite:///{tmp_path / 'm2.db'}") as (factory, incident):
        with factory() as session:  # an existing row makes sequence 1 collide
            session.add(
                RunEvidenceManifestRow(
                    run_id="run-1", sequence=1, source_type="LOG", source_id="999"
                )
            )
            session.commit()
        with pytest.raises(Exception):  # noqa: B017 - the unique violation
            build_manifest(factory, _request(incident), timestamp=T0)
        assert _counts(factory) == (1, 0)


@pytest.mark.postgres
def test_a_late_commit_after_the_manifest_snapshot_is_not_part_of_the_run(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _world(postgres_url) as (factory, incident):
        real = AlertRepository.ids_for_incident

        def first_query_then_late_commit(self: Any, incident_id: object) -> list[object]:
            result = real(self, incident_id)  # the transaction's snapshot is now taken
            with factory() as other:  # a writer commits a matching row meanwhile
                other.add(
                    EventVersionRow(
                        namespace="sre-demo",
                        involved_kind="Pod",
                        involved_name="p",
                        dedup_key=f"late-{uuid4()}",
                        event_at=T0 + timedelta(minutes=2),
                        observed_at=T0 + timedelta(minutes=2),
                        body={"metadata": {"uid": "late"}, "reason": "BackOff"},
                    )
                )
                other.commit()
            return result

        monkeypatch.setattr(AlertRepository, "ids_for_incident", first_query_then_late_commit)
        entries = build_manifest(factory, _request(incident), timestamp=datetime.now(UTC))

        with factory() as session:
            late = session.scalars(
                select(EventVersionRow.version_id).where(EventVersionRow.dedup_key.like("late-%"))
            ).one()
            first = session.scalars(
                select(EventVersionRow.version_id).where(EventVersionRow.dedup_key == "first")
            ).one()
        events = {e.source_id for e in entries if e.source_type == "EVENT_VERSION"}
        assert str(first) in events  # committed before the snapshot: in
        assert str(late) not in events  # committed after it: out
