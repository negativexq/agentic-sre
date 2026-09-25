"""M19-3.14: a replay source rebuilt from one run's boundary, manifest and snapshot only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_evidence_manifest import POD as _POD
from test_evidence_manifest import WARNING as _WARNING
from test_evidence_manifest import _run
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

import packages.rca.live as live_module
import packages.rca.replay as replay_module
import packages.storage.manifest as storage_manifest_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.investigation.environment import (
    LokiInvestigationBackend,
    PrometheusInvestigationBackend,
    TempoInvestigationBackend,
)
from packages.rca.live import KubernetesClusterReader, LiveSource
from packages.rca.model import EntityRef, LogRecord
from packages.rca.provider_adapter import ProviderAdapter
from packages.rca.replay import ReplayProviderAdapter, ReplaySource
from packages.storage.manifest import (
    ManifestAlertPayloadMissing,
    ManifestRequest,
    ReplayDataError,
    build_manifest,
    load_manifest,
)
from packages.storage.models import (
    AlertRow,
    IncidentEventRow,
    IncidentRow,
    LifecycleObservationRow,
    LogObservationRow,
    RunEvidenceManifestRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import (
    DiagnosisRepository,
    EventRepository,
    LifecycleRepository,
    LogObservationRepository,
    ObjectVersionRepository,
    SnapshotCycleRepository,
)

WINDOW_END = T0 + timedelta(minutes=30)
INSIDE = T0 + timedelta(minutes=20)  # plausible for the run's window, but not a member
LATE = T0 + timedelta(minutes=40)  # after the boundary
POD: dict[str, Any] = _POD
WARNING: dict[str, Any] = _WARNING


def _bomb(name: str) -> Any:
    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError(f"replay must not call {name}")

    return explode


@pytest.fixture
def run(setup: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:  # noqa: F811
    factory, incident_id, run_id, seen = _run(setup, monkeypatch)
    go_offline(monkeypatch)
    return factory, incident_id, run_id, seen.source


def go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """After capture: any Kubernetes reader construction/call, clock read or provider read fails."""
    monkeypatch.setattr(KubernetesClusterReader, "__init__", _bomb("KubernetesClusterReader()"))
    for method in ("_client", "list_objects", "list_events"):
        monkeypatch.setattr(KubernetesClusterReader, method, _bomb(method))
    monkeypatch.setattr(DiagnosisService, "snapshot_result", _bomb("snapshot_result"))

    class NoClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            raise AssertionError("replay must not read the clock")

        @classmethod
        def utcnow(cls) -> Any:
            raise AssertionError("replay must not read the clock")

    for module in (live_module, replay_module, storage_manifest_module):
        monkeypatch.setattr(module, "datetime", NoClock)
    for name in (
        "query_resource_pressure",
        "query_traffic",
        "query_tempo",
        "query_loki",
        "query_loki_with_read_id",
    ):
        monkeypatch.setattr(ProviderAdapter, name, _bomb(name))


def _replay(factory: sessionmaker[Session], run_id: str) -> ReplaySource:
    return ReplaySource.from_run(run_id, session_factory=factory)


def _boundary(factory: sessionmaker[Session], run_id: str) -> IncidentEventRow:
    with factory() as session:
        return next(
            row
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
            if row.payload.get("run_id") == run_id
        )


def _set_boundary(factory: sessionmaker[Session], run_id: str, payload: dict[str, Any]) -> None:
    event_id = _boundary(factory, run_id).event_id
    with factory() as session:
        row = session.get(IncidentEventRow, event_id)
        assert row is not None
        row.payload = payload
        session.commit()


def _member_ids(factory: sessionmaker[Session], run_id: str, source_type: str) -> set[str]:
    with factory() as session:
        return {e.source_id for e in load_manifest(session, run_id) if e.source_type == source_type}


def test_replay_matches_the_live_run_from_persisted_state_only(
    run: tuple[Any, ...],
) -> None:
    factory, incident_id, run_id, live = run
    replay = _replay(factory, run_id)
    assert isinstance(live, LiveSource)
    assert replay.incident_id() == live.incident_id() == str(incident_id)
    assert replay.observation_cutoff() == live.observation_cutoff() == WINDOW_END
    assert replay.alerts() == live.alerts()
    assert replay.object_history() == live.object_history()
    assert replay.events() == live.events()
    assert replay.error_logs() == live.error_logs()
    assert replay.logs("payment-service") == live.logs("payment-service")
    assert replay.pod_status_observations() == live.pod_status_observations()
    assert replay.pod_status_observations()  # lifecycle evidence is really present
    assert replay.traffic_observations() == live.traffic_observations()
    assert replay.trace_observations() == live.trace_observations()
    assert [(e.source_type, e.source_id) for e in replay.manifest] == [
        (e.source_type, e.source_id) for e in load_manifest(factory(), run_id)
    ]
    assert any(e.source_type == "CHANGE" for e in replay.manifest)  # kept as provenance


def test_boundary_window_end_and_cycle_are_used_exactly(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    payload = dict(_boundary(factory, run_id).payload)
    with factory() as session:
        cycle_id = session.scalars(select(SnapshotCycleRow.cycle_id)).one()
    # A boundary value no timestamp in the evidence could produce.
    moved = WINDOW_END + timedelta(seconds=7, microseconds=123)
    _set_boundary(factory, run_id, {**payload, "window_end": moved.isoformat()})
    replay = _replay(factory, run_id)
    assert replay.window_end == moved
    assert replay.observation_cutoff() == moved
    assert replay.snapshot_cycle_id == cycle_id == payload["snapshot_cycle_id"]


def test_non_member_and_late_rows_never_reach_replay(
    run: tuple[Any, ...],
) -> None:
    factory, incident_id, run_id, live = run
    before = _replay(factory, run_id)
    changed_pod = {**POD, "spec": {"nodeName": "late-node"}}
    late_event = {
        **WARNING,
        "metadata": {**WARNING["metadata"], "uid": "k8s-event-late"},
        "reason": "Killing",
        "lastTimestamp": LATE.isoformat(),
    }
    inside_event = {
        **WARNING,
        "metadata": {**WARNING["metadata"], "uid": "k8s-event-inside"},
        "reason": "Unhealthy",
    }
    with factory() as session:
        ObjectVersionRepository(session).record(changed_pod, INSIDE)
        ObjectVersionRepository(session).record({**changed_pod, "spec": {"x": 1}}, LATE)
        EventRepository(session).record(inside_event, INSIDE)
        EventRepository(session).record(late_event, LATE)
        LogObservationRepository(session).record(
            incident_id,
            [
                LogRecord(
                    service="payment-service",
                    at=at,
                    severity="error",
                    message=f"non-member {at.isoformat()}",
                    evidence_id=f"loki:payment-service:{at.minute}:9",
                )
                for at in (INSIDE, LATE)
            ],
            INSIDE,
        )
        template = session.scalars(select(LifecycleObservationRow)).first()
        assert template is not None
        for at in (INSIDE, LATE):
            LifecycleRepository(session).append(
                namespace=template.namespace,
                kind=template.kind,
                name=template.name,
                instance_uid=template.instance_uid,
                type=template.type,
                observed_at=at,
                source="collector",
                payload=dict(template.payload),
            )
        session.commit()
    after = _replay(factory, run_id)
    assert after.object_history() == before.object_history() == live.object_history()
    assert after.events() == before.events() == live.events()
    assert after.error_logs() == before.error_logs() == live.error_logs()
    assert after.pod_status_observations() == before.pod_status_observations()
    pod = EntityRef(kind="Pod", name=POD["metadata"]["name"], namespace="sre-demo")
    assert all(v.observed_at <= WINDOW_END for v in after.object_history()[pod])
    assert {e.reason for e in after.events()} == {"BackOff"}
    assert not any("non-member" in item.message for item in after.error_logs())


def test_object_versions_events_lifecycle_and_logs_are_exactly_manifest_rows(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    replay = _replay(factory, run_id)
    journal_ids = {
        v.evidence_id.removeprefix("journal:")
        for versions in replay.object_history().values()
        for v in versions
        if v.evidence_id.startswith("journal:")
    }
    assert journal_ids <= _member_ids(factory, run_id, "OBJECT_VERSION")
    assert {e.evidence_id for e in replay.events()} == {
        f"event:{i}" for i in _member_ids(factory, run_id, "EVENT_VERSION")
    }
    with factory() as session:
        for event in replay.events():  # each id resolves to its persisted row
            version_id = int(event.evidence_id.removeprefix("event:"))
            assert EventRepository(session).bodies([version_id])
        lifecycle = set(
            session.scalars(
                select(LifecycleObservationRow.evidence_id).where(
                    LifecycleObservationRow.observation_id.in_(
                        [int(i) for i in _member_ids(factory, run_id, "LIFECYCLE")]
                    )
                )
            )
        )
        logs = set(
            session.scalars(
                select(LogObservationRow.evidence_id).where(
                    LogObservationRow.observation_id.in_(
                        [int(i) for i in _member_ids(factory, run_id, "LOG")]
                    )
                )
            )
        )
    assert {o.evidence_id for o in replay.pod_status_observations()} <= lifecycle
    assert lifecycle and {r.evidence_id for r in replay.error_logs()} == logs


def test_alerts_come_from_the_frozen_manifest_payload(
    run: tuple[Any, ...],
) -> None:
    factory, incident_id, run_id, live = run
    with factory() as session:
        row = session.scalars(select(AlertRow).where(AlertRow.incident_id == incident_id)).one()
        row.status = "RESOLVED"
        row.alert_name = "RenamedLater"
        row.labels = {**row.labels, "late": "label"}
        session.commit()
    replay = _replay(factory, run_id)
    assert replay.alerts() == live.alerts()
    assert [a.name for a in replay.alerts()] == ["HighLatency"]
    assert "late" not in replay.alerts()[0].labels


def test_manifest_alert_without_payload_fails_without_row_fallback(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    with factory() as session:
        (entry,) = session.scalars(
            select(RunEvidenceManifestRow).where(
                RunEvidenceManifestRow.run_id == run_id,
                RunEvidenceManifestRow.source_type == "ALERT",
            )
        ).all()
        # Simulate a pre-3.6a manifest row; bypasses the ORM evidence guard.
        session.execute(
            update(RunEvidenceManifestRow)
            .where(RunEvidenceManifestRow.manifest_entry_id == entry.manifest_entry_id)
            .values(payload=None)
        )
        session.commit()
    with pytest.raises(ManifestAlertPayloadMissing):
        _replay(factory, run_id)


def test_replay_uses_the_boundary_cycle_not_a_newer_one(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    before = _replay(factory, run_id)
    newer_body = {**POD, "metadata": {**POD["metadata"], "name": "newer-pod", "uid": "newer"}}
    with factory() as session:
        newer = SnapshotCycleRepository(session).record(
            run_id=run_id,
            started_at=LATE,
            observed_at=LATE,
            completed_at=LATE,
            completed_scopes=[],
            failed_scopes=[],
            objects=[newer_body],
        )
        session.commit()
    replay = _replay(factory, run_id)
    assert newer != replay.snapshot_cycle_id == before.snapshot_cycle_id
    assert replay.snapshot_objects == before.snapshot_objects
    assert replay.snapshot_objects and newer_body not in replay.snapshot_objects
    assert all(
        not v.evidence_id.startswith(f"snapshot:{newer}:")
        for versions in replay.object_history().values()
        for v in versions
    )


def test_boundary_and_manifest_cycle_disagreement_fails(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    with factory() as session:
        other = SnapshotCycleRepository(session).record(
            run_id=run_id,
            started_at=LATE,
            observed_at=LATE,
            completed_at=LATE,
            completed_scopes=[],
            failed_scopes=[],
            objects=[],
        )
        session.commit()
    payload = dict(_boundary(factory, run_id).payload)
    _set_boundary(factory, run_id, {**payload, "snapshot_cycle_id": other})
    with pytest.raises(ReplayDataError, match="disagrees with manifest"):
        _replay(factory, run_id)
    _set_boundary(factory, run_id, {**payload, "snapshot_cycle_id": None})
    with pytest.raises(ReplayDataError, match="disagrees with manifest"):
        _replay(factory, run_id)


def test_snapshot_cycle_of_another_run_fails(
    run: tuple[Any, ...],
) -> None:
    factory, incident_id, run_id, _ = run
    with factory() as session:
        cycle_id = session.scalars(select(SnapshotCycleRow.cycle_id)).one()
        correlation_id = _boundary(factory, run_id).correlation_id
    # A consistent boundary+manifest for a second run that names run_id's cycle.
    build_manifest(
        factory,
        ManifestRequest(
            run_id="forged-run",
            incident_id=incident_id,
            correlation_id=correlation_id,
            starts_at=T0,
            ends_at=WINDOW_END,
            window_end=WINDOW_END,
            namespaces=frozenset({"sre-demo"}),
            journal_namespaces=frozenset({"sre-demo"}),
            snapshot_cycle_id=cycle_id,
            listed_objects=0,
            provider_capabilities=(),
        ),
        timestamp=WINDOW_END,
    )
    with pytest.raises(ReplayDataError, match="belongs to run"):
        _replay(factory, "forged-run")


def test_runs_do_not_leak_into_each_other(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, incident_id, first_run, _ = _run(setup, monkeypatch)
    first = ReplaySource.from_run(first_run, session_factory=factory)
    _, cluster, clock, _ = setup
    cluster.objects.append(
        {**POD, "metadata": {**POD["metadata"], "name": "second-pod", "uid": "pod-uid-2"}}
    )
    clock.now = T0 + timedelta(minutes=35)
    DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    ).run(incident_id)
    with factory() as session:
        second_run = DiagnosisRepository(session).latest_run_id(incident_id)
    assert second_run is not None and second_run != first_run
    go_offline(monkeypatch)
    again = ReplaySource.from_run(first_run, session_factory=factory)
    second = ReplaySource.from_run(second_run, session_factory=factory)
    assert again.snapshot_cycle_id == first.snapshot_cycle_id != second.snapshot_cycle_id
    assert again.object_history() == first.object_history()
    assert again.manifest == first.manifest
    second_pod = EntityRef(kind="Pod", name="second-pod", namespace="sre-demo")
    assert second_pod not in again.object_history() and second_pod in second.object_history()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "window_end"}, id="no-end"),
        pytest.param(lambda p: {**p, "window_end": "not-a-time"}, id="bad-end"),
        pytest.param(lambda p: {**p, "window_end": "2026-09-17T10:30:00"}, id="naive-end"),
        pytest.param(
            lambda p: {k: v for k, v in p.items() if k != "snapshot_cycle_id"}, id="no-cycle"
        ),
        pytest.param(lambda p: {**p, "snapshot_cycle_id": "1"}, id="string-cycle"),
    ],
)
def test_malformed_boundary_fails(
    run: tuple[Any, ...],
    mutate: Any,
) -> None:
    factory, _, run_id, _ = run
    _set_boundary(factory, run_id, mutate(dict(_boundary(factory, run_id).payload)))
    with pytest.raises(ReplayDataError):
        _replay(factory, run_id)


def test_missing_and_duplicated_boundaries_fail(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    boundary = _boundary(factory, run_id)
    with factory() as session:
        session.add(
            IncidentEventRow(
                event_id=UUID(int=boundary.event_id.int ^ 1),
                incident_id=boundary.incident_id,
                sequence=10_000,
                event_type=boundary.event_type,
                timestamp=boundary.timestamp,
                payload=dict(boundary.payload),
                correlation_id=boundary.correlation_id,
            )
        )
        session.commit()
    with pytest.raises(ReplayDataError, match="2 EVIDENCE_GATHERED"):
        _replay(factory, run_id)
    with factory() as session:
        session.execute(
            delete(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
        )
        session.commit()
    with pytest.raises(ReplayDataError, match="0 EVIDENCE_GATHERED"):
        _replay(factory, run_id)


def test_missing_member_row_fails_instead_of_shrinking_evidence(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    (log_id,) = _member_ids(factory, run_id, "LOG")
    with factory() as session:
        # Retention-style Core delete, which the ORM guard does not see.
        session.execute(
            delete(LogObservationRow).where(LogObservationRow.observation_id == int(log_id))
        )
        session.commit()
    with pytest.raises(ReplayDataError, match="LOG"):
        _replay(factory, run_id)


def test_resolved_run_without_cycle_replays_journal_only(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, clock, incident_id = setup
    with factory() as session:
        incident = session.get(IncidentRow, incident_id)
        assert incident is not None
        incident.status = "RESOLVED"
        session.commit()
    clock.now = WINDOW_END
    DiagnosisService(session_factory=factory, namespaces=("sre-demo",), clock=clock).run(
        incident_id
    )
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    go_offline(monkeypatch)
    assert _boundary(factory, run_id).payload["snapshot_cycle_id"] is None
    replay = _replay(factory, run_id)
    assert replay.snapshot_cycle_id is None and replay.snapshot_objects == ()
    assert not any(e.source_type == "SNAPSHOT_CYCLE" for e in replay.manifest)


def test_provider_backed_reads_follow_the_frozen_capabilities(
    run: tuple[Any, ...],
) -> None:
    factory, _, run_id, _ = run
    replay = _replay(factory, run_id)
    pod = EntityRef(kind="Pod", name=POD["metadata"]["name"], namespace="sre-demo")
    # The capture ran with Loki only.
    assert replay.provider_capabilities == ("logs",)
    # Prometheus was unsupported live, so no read happened there either: no tape row.
    cursor_before = replay.provider_adapter.next_sequence
    assert replay.resource_pressure([pod], T0) == []
    assert replay.provider_adapter.next_sequence == cursor_before
    chain: list[Any] = []
    current: Any = replay.investigation_backend()
    while current is not None:
        chain.append(current)
        current = getattr(current, "base", None)
    kinds = {type(item) for item in chain}
    assert LokiInvestigationBackend in kinds
    assert PrometheusInvestigationBackend not in kinds
    assert TempoInvestigationBackend not in kinds
    adapters = [item.provider_adapter for item in chain if hasattr(item, "provider_adapter")]
    assert adapters and all(isinstance(item, ReplayProviderAdapter) for item in adapters)
    assert replay.supports("history") and replay.supports("events") and replay.supports("logs")
