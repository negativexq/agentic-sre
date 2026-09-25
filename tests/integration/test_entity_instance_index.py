"""M19-2.5: each watch cycle keeps the materialized exact-instance index current."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import ListingFailure, ListingScope, ObjectListing
from packages.storage.database import create_session_factory
from packages.storage.models import Base, EntityInstanceRow

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SCOPES = frozenset({ListingScope("sre-demo", "Pod"), ListingScope("sre-demo", "Deployment")})


def _object(kind: str, name: str, uid: str | None, **metadata: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": kind,
        "metadata": {"name": name, "namespace": "sre-demo", **metadata},
    }
    if uid is not None:
        body["metadata"]["uid"] = uid
    return body


def _pod(uid: str) -> dict[str, Any]:
    return _object(
        "Pod",
        "web-0",
        uid,
        creationTimestamp="2026-09-25T11:40:00Z",
        ownerReferences=[
            {"kind": "ReplicaSet", "name": "web-old", "uid": "rs-old"},
            {"kind": "StatefulSet", "name": "web", "uid": "sts-1", "controller": True},
        ],
    )


class Cluster:
    def __init__(self) -> None:
        self.objects: list[dict[str, Any]] = []
        self.pods_failed = False

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        del namespaces
        if self.pods_failed:
            return ObjectListing(
                tuple(o for o in copy.deepcopy(self.objects) if o["kind"] != "Pod"),
                frozenset({ListingScope("sre-demo", "Deployment")}),
                (ListingFailure(ListingScope("sre-demo", "Pod"), "timeout"),),
            )
        return ObjectListing(tuple(copy.deepcopy(self.objects)), SCOPES)

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
    engine = create_engine(f"sqlite:///{tmp_path / 'index.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _index(factory: sessionmaker[Session]) -> dict[tuple[str, str], EntityInstanceRow]:
    with factory() as session:
        return {
            (row.kind, row.uid): row for row in session.scalars(select(EntityInstanceRow)).all()
        }


def test_cycles_upsert_instances_and_mark_tombstoned_ones_deleted(
    factory: sessionmaker[Session],
) -> None:
    cluster, clock = Cluster(), Clock()
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        evidence_namespaces=(),
        reader=cluster,
        clock=clock,
    )
    deployment = _object("Deployment", "api", "dep-1")
    cluster.objects = [_pod("uid-a"), deployment, _object("Deployment", "no-uid", None)]
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=15)
    service.snapshot_result()

    index = _index(factory)
    assert set(index) == {("Pod", "uid-a"), ("Deployment", "dep-1")}
    pod = index[("Pod", "uid-a")]
    assert (pod.first_observed_at, pod.last_observed_at) == (T0, T0 + timedelta(seconds=15))
    assert (pod.owner_kind, pod.owner_name, pod.owner_uid) == ("StatefulSet", "web", "sts-1")
    assert pod.created_at == datetime(2026, 9, 25, 11, 40, tzinfo=UTC)
    assert pod.deleted_observed_at is None

    # A failed Pod listing is missing evidence, not a deletion.
    clock.now = T0 + timedelta(seconds=30)
    cluster.pods_failed = True
    service.snapshot_result()
    assert _index(factory)[("Pod", "uid-a")].deleted_observed_at is None

    # The Pod is gone from a complete listing and a new UID took its name.
    clock.now = T0 + timedelta(seconds=45)
    cluster.pods_failed = False
    cluster.objects = [deployment]
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=60)
    cluster.objects = [deployment, _pod("uid-b")]
    service.snapshot_result()

    index = _index(factory)
    assert index[("Pod", "uid-a")].deleted_observed_at == T0 + timedelta(seconds=45)
    assert index[("Pod", "uid-b")].deleted_observed_at is None
    assert index[("Pod", "uid-b")].name == "web-0"
    assert index[("Deployment", "dep-1")].deleted_observed_at is None
