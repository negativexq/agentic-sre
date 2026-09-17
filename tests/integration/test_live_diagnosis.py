"""Live diagnosis path: journal, cluster reader, API, and web pages, with fakes."""

from __future__ import annotations

import copy
import io
import json
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from apps.control_plane.main import create_app
from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.live import LokiLogReader
from packages.rca.model import Confidence, EntityRef, FindingKind, LogRecord
from packages.storage.database import create_session_factory
from packages.storage.models import AlertRow, Base, IncidentRow, LogObservationRow
from packages.storage.repositories import IncidentRepository, ObjectVersionRepository

T0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def _deployment(delay: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "payment-service",
            "namespace": "sre-demo",
            "resourceVersion": delay,
            "labels": {"app": "payment-service"},
        },
        "spec": {
            "selector": {"matchLabels": {"app": "payment-service"}},
            "template": {
                "metadata": {"labels": {"app": "payment-service"}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "payment:1",
                            "env": [{"name": "FAULT_PAYMENT_DELAY_MS", "value": delay}],
                        }
                    ]
                },
            },
        },
        "status": {"readyReplicas": 1},
    }


def _order_service() -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": "order-service", "namespace": "sre-demo"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "env": [
                                {"name": "PAYMENT_URL", "value": "http://payment-service:8000"}
                            ],
                        }
                    ]
                }
            }
        },
    }


class FakeCluster:
    def __init__(self) -> None:
        self.objects = [
            _deployment("0"),
            _order_service(),
            {
                "kind": "Service",
                "metadata": {"name": "payment-service", "namespace": "sre-demo"},
                "spec": {"selector": {"app": "payment-service"}},
            },
            {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "payment-service-7d9f",
                    "namespace": "sre-demo",
                    "ownerReferences": [{"kind": "Deployment", "name": "payment-service"}],
                },
            },
            {
                "kind": "Pod",
                "metadata": {
                    "name": "payment-service-7d9f-x2x4q",
                    "namespace": "sre-demo",
                    "labels": {"app": "payment-service"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "payment-service-7d9f"}],
                },
            },
        ]
        self.events: list[dict[str, Any]] = []

    def list_objects(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return copy.deepcopy(self.objects)

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return copy.deepcopy(self.events)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeLogs:
    def __init__(self, records: list[LogRecord]) -> None:
        self.records = records
        self.calls = 0
        self.unavailable = False

    def error_logs(
        self, services: Sequence[str], starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord]:
        del services, starts_at, ends_at
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("Loki unavailable")
        return copy.deepcopy(self.records)


@pytest.fixture
def setup(tmp_path: Path) -> Generator[tuple[sessionmaker[Session], FakeCluster, Clock, UUID]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="Checkout latency",
        created_at=T0 + timedelta(minutes=12),
        updated_at=T0 + timedelta(minutes=12),
    )
    alert = Alert(
        alert_name="HighLatency",
        service="order-service",
        namespace="sre-demo",
        cluster="kind",
        starts_at=T0 + timedelta(minutes=11),
        fingerprint="fp-1",
        status=AlertStatus.FIRING,
        source=AlertSource.PROMETHEUS,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
        session.add(
            AlertRow(
                alert_id=alert.alert_id,
                incident_id=incident.incident_id,
                alert_name=alert.alert_name,
                service=alert.service,
                namespace=alert.namespace,
                cluster=alert.cluster,
                starts_at=alert.starts_at,
                ends_at=None,
                labels=alert.labels,
                annotations=alert.annotations,
                fingerprint=alert.fingerprint,
                status=alert.status.value,
                source=alert.source.value,
            )
        )
        session.commit()
    yield factory, FakeCluster(), Clock(), incident.incident_id
    engine.dispose()


def test_journal_stores_only_content_changes(setup: Any) -> None:
    factory, _cluster, _clock, _incident = setup
    with factory() as session:
        repository = ObjectVersionRepository(session)
        assert repository.record(_deployment("0"), T0) is True
        # resourceVersion and status differ, desired state does not
        changed_bookkeeping = _deployment("0")
        changed_bookkeeping["metadata"]["resourceVersion"] = "99"
        changed_bookkeeping["status"] = {"readyReplicas": 0}
        assert repository.record(changed_bookkeeping, T0 + timedelta(minutes=1)) is False
        assert repository.record(_deployment("5000"), T0 + timedelta(minutes=2)) is True
        history = repository.history(
            namespaces={"sre-demo"},
            starts_at=T0 + timedelta(minutes=1),
            ends_at=T0 + timedelta(hours=1),
        )
    assert [entry.observed_at for entry in history] == [T0, T0 + timedelta(minutes=2)]


def test_event_listing_failure_keeps_object_journal_consistent(setup: Any) -> None:
    factory, cluster, clock, _incident = setup
    original = cluster.list_events

    def fail_events(namespaces: Sequence[str]) -> list[dict[str, Any]]:
        del namespaces
        raise RuntimeError("event API unavailable")

    cluster.list_events = fail_events
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5
    with factory() as session:
        repository = ObjectVersionRepository(session)
        history = repository.history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=1)
        )
    cluster.list_events = original
    assert len(history) == 5


def test_journal_records_a_rollback_that_repeats_earlier_content(setup: Any) -> None:
    """A -> B -> A must not collide on the old (object_key, content_hash) uniqueness."""
    factory, _cluster, _clock, _incident = setup
    with factory() as session:
        repository = ObjectVersionRepository(session)
        assert repository.record(_deployment("0"), T0) is True
        assert repository.record(_deployment("5000"), T0 + timedelta(minutes=1)) is True
        # Rollback: content equals the very first version again.
        assert repository.record(_deployment("0"), T0 + timedelta(minutes=2)) is True
        history = repository.history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(hours=1)
        )
    assert [entry.observed_at for entry in history] == [
        T0,
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
    ]
    assert [entry.lifecycle.value for entry in history] == ["OBSERVED", "UPDATED", "UPDATED"]


def test_deleted_object_is_tombstoned_and_can_be_recreated(setup: Any) -> None:
    factory, _cluster, _clock, _incident = setup
    with factory() as session:
        repository = ObjectVersionRepository(session)
        assert repository.record(_deployment("0"), T0) is True
        assert (
            repository.tombstone("sre-demo/Deployment/payment-service", T0 + timedelta(minutes=1))
            is True
        )
        # Tombstoning twice in a row is a no-op.
        assert (
            repository.tombstone("sre-demo/Deployment/payment-service", T0 + timedelta(minutes=2))
            is False
        )
        # A recreation after a tombstone is recorded even with the same content.
        assert repository.record(_deployment("0"), T0 + timedelta(minutes=3)) is True
        history = repository.history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(hours=1)
        )
    assert [entry.lifecycle.value for entry in history] == ["OBSERVED", "DELETED", "CREATED"]


def test_watched_config_change_is_diagnosed_through_the_api(setup: Any) -> None:
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5
    assert service.snapshot() == 0
    clock.now = T0 + timedelta(minutes=10)
    cluster.objects[0] = _deployment("5000")
    assert service.snapshot() == 1
    clock.now = T0 + timedelta(minutes=13)

    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        missing = client.get(f"/api/v1/incidents/{incident_id}/diagnosis")
        assert missing.status_code == 404
        created = client.post(f"/api/v1/incidents/{incident_id}/diagnosis").json()
        assert created["root_cause"] == {
            "kind": "Deployment",
            "name": "payment-service",
            "namespace": "sre-demo",
        }
        assert created["confidence"] == "VERIFIED"
        assert created["evidence"][0]["kind"] == "SPEC_CHANGE"
        assert "rollout undo" in created["remediation"][0]["command"]
        stored = client.get(f"/api/v1/incidents/{incident_id}/diagnosis").json()
        assert stored == created
        index = client.get("/")
        assert index.status_code == 200
        assert "sre-demo/Deployment/payment-service" in index.text
        page = client.get(f"/incidents/{incident_id}")
        assert "FAULT_PAYMENT_DELAY_MS" in page.text
        assert "not executed" in page.text
        assert client.post("/api/v1/cluster/snapshot").json() == {"stored_versions": 0}


def test_deleted_object_becomes_a_verified_root_cause(setup: Any) -> None:
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5
    clock.now = T0 + timedelta(minutes=10)
    del cluster.objects[1]  # order-service Deployment, the alerting component
    assert service.snapshot() == 1
    clock.now = T0 + timedelta(minutes=13)
    diagnosis = service.run(incident_id)
    assert diagnosis.root_cause == EntityRef.parse("sre-demo/Deployment/order-service")
    assert diagnosis.evidence[0].kind is FindingKind.OBJECT_DELETED
    assert diagnosis.confidence is Confidence.VERIFIED


def test_diagnosis_without_cluster_access_is_explicit(setup: Any) -> None:
    factory, _cluster, clock, incident_id = setup
    service = DiagnosisService(session_factory=factory, namespaces=("sre-demo",), clock=clock)
    diagnosis = service.run(incident_id)
    assert diagnosis.root_cause is None
    assert diagnosis.summary.startswith("No change")


def test_open_diagnosis_persists_and_deduplicates_log_observations(setup: Any) -> None:
    factory, cluster, clock, incident_id = setup
    logs = FakeLogs(
        [
            LogRecord(
                service="order-service",
                at=T0 + timedelta(minutes=12),
                severity="ERROR",
                message="payment timeout",
                evidence_id="loki:order-service:12:0",
            )
        ]
    )
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        log_reader=logs,
        clock=clock,
    )
    clock.now = T0 + timedelta(minutes=13)
    service.run(incident_id)
    service.run(incident_id)
    with factory() as session:
        rows = session.query(LogObservationRow).all()
    assert logs.calls == 2
    assert len(rows) == 1
    assert rows[0].message == "payment timeout"


def test_resolved_replay_uses_persisted_logs_when_loki_is_unavailable(
    setup: Any,
) -> None:
    factory, cluster, clock, incident_id = setup
    early = LogRecord(
        service="order-service",
        at=T0 + timedelta(minutes=12),
        severity="ERROR",
        message="payment timeout",
        evidence_id="loki:order-service:12:0",
    )
    logs = FakeLogs([early])
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        log_reader=logs,
        clock=clock,
    )
    clock.now = T0 + timedelta(minutes=13)
    service.run(incident_id)
    resolved_at = T0 + timedelta(minutes=15)
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = resolved_at
        session.commit()

    logs.records = [
        LogRecord(
            service="order-service",
            at=T0 + timedelta(minutes=30),
            severity="ERROR",
            message="post-resolution timeout",
            evidence_id="loki:order-service:30:0",
        )
    ]
    logs.unavailable = True
    clock.now = T0 + timedelta(minutes=30)
    diagnosis = service.run(incident_id)
    assert logs.calls == 1
    assert all(item.summary != "post-resolution timeout" for item in diagnosis.evidence)
    with factory() as session:
        rows = session.query(LogObservationRow).all()
    assert len(rows) == 1 and rows[0].message == "payment timeout"


def test_loki_reader_parses_streams_and_bounds_query() -> None:
    captured: dict[str, Any] = {}

    def opener(request: Any, timeout: float) -> io.BytesIO:
        captured["url"] = request.full_url
        payload = {
            "data": {
                "result": [
                    {
                        "stream": {"service_name": "order-service", "level": "error"},
                        "values": [[str(int(T0.timestamp() * 1e9)), "payment timeout"]],
                    }
                ]
            }
        }
        return io.BytesIO(json.dumps(payload).encode())

    reader = LokiLogReader("http://loki:3100", opener=opener)
    records = reader.error_logs(["order-service", "bad name!"], T0, T0 + timedelta(minutes=5))
    assert [(r.service, r.message, r.at) for r in records] == [
        ("order-service", "payment timeout", T0)
    ]
    assert "query_range" in captured["url"] and "limit=500" in captured["url"]
    assert "order-service" in captured["url"] and "bad" not in captured["url"]
    assert reader.error_logs(["$(x)"], T0, T0) == []


def test_watch_loop_snapshots_until_stopped(setup: Any) -> None:
    import threading

    factory, cluster, clock, _incident = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    stop = threading.Event()
    calls: list[int] = []
    original = service.snapshot

    def counting() -> int:
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        return original()

    service.snapshot = counting  # type: ignore[method-assign]
    service.watch(stop, interval_seconds=0.001)
    assert len(calls) == 2


def test_llm_investigator_shares_one_client_and_budget_across_incidents(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh OpenAIClient per incident would reset SRE_LLM_MAX_CALLS every time."""
    factory, _cluster, _clock, _incident = setup
    monkeypatch.setenv("SRE_LLM_ENABLED", "true")
    monkeypatch.setenv("SRE_LLM_MAX_CALLS", "5")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    service = service_from_environment(factory)
    first = service.investigator_factory()
    second = service.investigator_factory()
    assert first is not None and second is not None
    assert first.client is second.client  # type: ignore[attr-defined]
    assert first.client.max_calls == 5  # type: ignore[attr-defined]


def test_resolved_incident_window_is_frozen_at_its_resolution(setup: Any) -> None:
    """Diagnosing a closed incident later must not pick up unrelated later changes."""
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5
    resolved_at = T0 + timedelta(minutes=15)
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = resolved_at
        session.commit()

    # A long time after resolution, something unrelated changes.
    clock.now = T0 + timedelta(hours=3)
    cluster.objects[0] = _deployment("5000")
    diagnosis_after_close = service.run(incident_id)
    assert diagnosis_after_close.root_cause is None
    assert diagnosis_after_close.summary.startswith("No change")

    # The same unrelated change must not have poisoned the shared journal for
    # a second, open incident either -- it should be dated at "now", not the
    # closed incident's frozen window.
    with factory() as session:
        history = ObjectVersionRepository(session).history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(hours=4)
        )
    assert any(entry.observed_at == T0 + timedelta(hours=3) for entry in history)


def test_resolved_replay_excludes_event_state_observed_after_resolution(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, cluster, clock, incident_id = setup
    event = {
        "kind": "Event",
        "metadata": {"name": "checkout-backoff.sre-demo", "namespace": "sre-demo", "uid": "e1"},
        "involvedObject": {
            "kind": "Pod",
            "name": "checkout-1",
            "namespace": "sre-demo",
            "uid": "p1",
        },
        "reason": "BackOff",
        "type": "Warning",
        "message": "back-off restarting failed container",
        "firstTimestamp": (T0 + timedelta(minutes=2)).isoformat(),
        "lastTimestamp": (T0 + timedelta(minutes=2)).isoformat(),
        "count": 1,
    }
    cluster.events = [event]
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 6
    resolved_at = T0 + timedelta(minutes=15)
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = resolved_at
        session.commit()

    late = dict(event)
    late["lastTimestamp"] = (T0 + timedelta(minutes=30)).isoformat()
    late["count"] = 20
    cluster.events = [late]
    clock.now = T0 + timedelta(minutes=30)
    captured: dict[str, list[Any]] = {}

    import importlib

    diagnosis_module = importlib.import_module("apps.control_plane.diagnosis")
    original = diagnosis_module.diagnose

    def spy(source: Any, **kwargs: Any) -> Any:
        captured["events"] = list(source.events())
        return original(source, **kwargs)

    monkeypatch.setattr(diagnosis_module, "diagnose", spy)
    service.run(incident_id)
    assert [item.count for item in captured["events"]] == [1]


def test_concurrent_snapshots_are_serialized(setup: Any) -> None:
    """The service's lock must keep two snapshot() calls from overlapping.

    Without it, two threads could both read the same object's stale "latest"
    version before either writes, and both decide it changed.
    """
    import threading
    import time

    factory, cluster, clock, _incident = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5

    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    original_list_objects = cluster.list_objects

    def slow_list_objects(namespaces: Any) -> Any:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        try:
            return original_list_objects(namespaces)
        finally:
            with counter_lock:
                active -= 1

    cluster.list_objects = slow_list_objects
    clock.now = T0 + timedelta(minutes=10)
    cluster.objects[0] = _deployment("5000")

    threads = [threading.Thread(target=service.snapshot) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert max_active == 1  # the reader was never entered by two threads at once
    with factory() as session:
        history = ObjectVersionRepository(session).history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(hours=1)
        )
    deployment_versions = [
        entry for entry in history if entry.object_key == "sre-demo/Deployment/payment-service"
    ]
    assert len(deployment_versions) == 2  # the initial version, then exactly one update


def test_resolved_incident_diagnosis_is_stable_across_reruns(setup: Any) -> None:
    """The Phase 1 gate: same incident, same snapshot -> same diagnosis, always."""
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    assert service.snapshot() == 5
    clock.now = T0 + timedelta(minutes=10)
    cluster.objects[0] = _deployment("5000")
    assert service.snapshot() == 1
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = T0 + timedelta(minutes=15)
        session.commit()

    clock.now = T0 + timedelta(minutes=20)
    first = service.run(incident_id)
    clock.now = T0 + timedelta(days=30)  # long after resolution, cluster drifts further
    cluster.objects[0] = _deployment("9999")
    second = service.run(incident_id)
    assert first == second


def test_events_persist_past_kubernetes_garbage_collection(setup: Any) -> None:
    """A warning event must still explain an incident after the cluster has
    garbage collected it (Kubernetes keeps events for about an hour)."""
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    warning_at = T0 + timedelta(minutes=2)
    cluster.events = [
        {
            "kind": "Event",
            "metadata": {"uid": "evt-1", "name": "order-service.warn"},
            "involvedObject": {
                "kind": "Deployment",
                "name": "order-service",
                "namespace": "sre-demo",
            },
            "reason": "BackOff",
            "type": "Warning",
            "message": "back-off restarting failed container",
            "firstTimestamp": warning_at.isoformat().replace("+00:00", "Z"),
            "lastTimestamp": warning_at.isoformat().replace("+00:00", "Z"),
            "count": 9,
        }
    ]
    assert service.snapshot() >= 1

    # Simulate the cluster garbage collecting the event.
    cluster.events = []
    clock.now = T0 + timedelta(minutes=13)
    diagnosis = service.run(incident_id)
    assert diagnosis.root_cause == EntityRef.parse("sre-demo/Deployment/order-service")
    assert diagnosis.evidence[0].kind is FindingKind.FAILURE_EVENT
    assert "BackOff" in diagnosis.evidence[0].summary


def test_resolved_incident_events_are_also_frozen_at_resolution(setup: Any) -> None:
    """The event journal must respect the same frozen window as objects."""
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    warning_at = T0 + timedelta(minutes=2)
    cluster.events = [
        {
            "kind": "Event",
            "metadata": {"uid": "evt-1", "name": "order-service.warn"},
            "involvedObject": {
                "kind": "Deployment",
                "name": "order-service",
                "namespace": "sre-demo",
            },
            "reason": "BackOff",
            "type": "Warning",
            "message": "back-off restarting failed container",
            "firstTimestamp": warning_at.isoformat().replace("+00:00", "Z"),
            "lastTimestamp": warning_at.isoformat().replace("+00:00", "Z"),
            "count": 9,
        }
    ]
    assert service.snapshot() >= 1
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = T0 + timedelta(minutes=15)
        session.commit()

    # A new, unrelated warning fires long after resolution.
    clock.now = T0 + timedelta(days=1)
    cluster.events = [
        {
            "kind": "Event",
            "metadata": {"uid": "evt-2", "name": "order-service.warn2"},
            "involvedObject": {
                "kind": "Deployment",
                "name": "order-service",
                "namespace": "sre-demo",
            },
            "reason": "Unrelated",
            "type": "Warning",
            "message": "something else, much later",
            "firstTimestamp": clock.now.isoformat().replace("+00:00", "Z"),
            "lastTimestamp": clock.now.isoformat().replace("+00:00", "Z"),
            "count": 1,
        }
    ]
    diagnosis = service.run(incident_id)
    assert diagnosis.evidence[0].kind is FindingKind.FAILURE_EVENT
    assert "BackOff" in diagnosis.evidence[0].summary
    assert "Unrelated" not in diagnosis.evidence[0].summary
