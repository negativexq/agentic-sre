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

from apps.control_plane.diagnosis import DiagnosisService
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
from packages.rca.model import Confidence, EntityRef, FindingKind
from packages.storage.database import create_session_factory
from packages.storage.models import AlertRow, Base
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
