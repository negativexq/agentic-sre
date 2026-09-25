"""M19-2.4: each watch cycle appends Pod lifecycle facts to the ledger."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import ListingFailure, ListingScope, ObjectListing
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import LifecycleRepository, ObjectVersionRepository

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
POD_SCOPE = ListingScope("sre-demo", "Pod")


def _pod(
    name: str, uid: str, *, ready: str = "True", since: str = "2026-09-25T11:50:00Z"
) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "sre-demo",
            "uid": uid,
            "creationTimestamp": "2026-09-25T11:40:00Z",
        },
        "spec": {"containers": [{"name": "app", "image": "app:1"}]},
        "status": {
            "conditions": [{"type": "Ready", "status": ready, "lastTransitionTime": since}],
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-09-25T11:41:00Z"}},
                    "lastState": {},
                }
            ],
        },
    }


class Cluster:
    """Reader double whose Pod listing can succeed or fail per cycle."""

    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = []
        self.pod_listing_failed = False

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        del namespaces
        if self.pod_listing_failed:
            return ObjectListing((), frozenset(), (ListingFailure(POD_SCOPE, "timeout"),))
        return ObjectListing(tuple(copy.deepcopy(self.pods)), frozenset({POD_SCOPE}))

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        del namespaces
        return []


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _service(factory: sessionmaker[Session], cluster: Cluster, clock: Clock) -> DiagnosisService:
    return DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        evidence_namespaces=(),
        reader=cluster,
        clock=clock,
    )


def _rows(factory: sessionmaker[Session], uid: str) -> list[tuple[str, float, str]]:
    with factory() as session:
        return [
            (item.type, (item.observed_at - T0).total_seconds(), item.source)
            for item in LifecycleRepository(session).list_for("sre-demo", "Pod", uid)
        ]


def test_three_cycles_record_transitions_dedupe_snapshots_and_tombstone_deletion(
    factory: sessionmaker[Session],
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)

    # Cycle 1: both Pods are seen for the first time.
    cluster.pods = [_pod("web-a", "uid-a"), _pod("web-b", "uid-b")]
    service.snapshot_result()
    # Cycle 2 (+15s): web-a unchanged; web-b went NotReady.
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = [
        _pod("web-a", "uid-a"),
        _pod("web-b", "uid-b", ready="False", since="2026-09-25T12:00:10Z"),
    ]
    service.snapshot_result()
    # Cycle 3 (+45s): web-b is gone from a complete listing; web-a unchanged.
    clock.now = T0 + timedelta(seconds=45)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    assert _rows(factory, "uid-a") == [
        ("OBSERVED", 0, "collector"),
        ("STATUS_SNAPSHOT", 0, "collector"),
        # +15s: same content within the interval, so no snapshot.
        ("STATUS_SNAPSHOT", 45, "collector"),
    ]
    assert _rows(factory, "uid-b") == [
        ("OBSERVED", 0, "collector"),
        ("STATUS_SNAPSHOT", 0, "collector"),
        ("READY_FALSE", 15, "collector"),
        ("STATUS_SNAPSHOT", 15, "collector"),
        ("DELETED", 45, "journal-tombstone"),
    ]


def test_failed_pod_listing_never_records_a_deletion(factory: sessionmaker[Session]) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    clock.now = T0 + timedelta(seconds=60)
    cluster.pod_listing_failed = True
    result = service.snapshot_result()

    assert result.failed_scopes
    assert [row[0] for row in _rows(factory, "uid-a")] == ["OBSERVED", "STATUS_SNAPSHOT"]
    with factory() as session:
        assert "sre-demo/Pod/web-a" in ObjectVersionRepository(session).live_keys({"sre-demo"})


def test_same_name_with_a_new_uid_is_a_new_instance(factory: sessionmaker[Session]) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-0", "uid-a")]
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = []
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=30)
    cluster.pods = [_pod("web-0", "uid-b", ready="False", since="2026-09-25T12:00:20Z")]
    service.snapshot_result()

    assert [row[0] for row in _rows(factory, "uid-a")] == [
        "OBSERVED",
        "STATUS_SNAPSHOT",
        "DELETED",
    ]
    assert [row[0] for row in _rows(factory, "uid-b")] == ["OBSERVED", "STATUS_SNAPSHOT"]


def test_a_restarted_collector_does_not_observe_known_instances_again(
    factory: sessionmaker[Session],
) -> None:
    cluster, clock = Cluster(), Clock()
    cluster.pods = [_pod("web-a", "uid-a")]
    _service(factory, cluster, clock).snapshot_result()

    clock.now = T0 + timedelta(seconds=10)
    _service(factory, cluster, clock).snapshot_result()  # a fresh process, same database

    assert [row[0] for row in _rows(factory, "uid-a")] == ["OBSERVED", "STATUS_SNAPSHOT"]


def test_pods_without_uid_or_outside_watched_namespaces_are_ignored(
    factory: sessionmaker[Session],
) -> None:
    cluster, clock = Cluster(), Clock()
    no_uid = _pod("web-x", "uid-x")
    del no_uid["metadata"]["uid"]
    elsewhere = _pod("web-y", "uid-y")
    elsewhere["metadata"]["namespace"] = "kube-system"
    cluster.pods = [no_uid, elsewhere]
    _service(factory, cluster, clock).snapshot_result()

    with factory() as session:
        ledger = LifecycleRepository(session)
        assert ledger.list_window({"sre-demo", "kube-system"}, T0, T0 + timedelta(hours=1)) == []
