"""M19-3.4: a diagnosis capture persists its snapshot cycle with full bodies."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import ListingFailure, ListingScope, LiveSource, ObjectListing
from packages.rca.model import EntityRef, ObjectVersion
from packages.storage.database import create_session_factory
from packages.storage.evidence_guard import AuthoritativeEvidenceMutation
from packages.storage.models import (
    Base,
    ObjectVersionRow,
    SnapshotCycleObjectRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import DiagnosisRepository

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
POD_SCOPE = ListingScope("shop", "Pod")
DEPLOYMENT_SCOPE = ListingScope("shop", "Deployment")
CHAOS_SCOPE = ListingScope("chaos-mesh", "NetworkChaos")

POD = {
    "kind": "Pod",
    "metadata": {"name": "web-0", "namespace": "shop", "uid": "uid-a"},
    "spec": {"containers": [{"name": "app", "image": "app:1"}]},
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [{"name": "app", "restartCount": 2}],
    },
}
DEPLOYMENT = {
    "kind": "Deployment",
    "metadata": {"name": "web", "namespace": "shop", "uid": "dep-1"},
    "spec": {"replicas": 1},
    "status": {"readyReplicas": 1},
}


class Cluster:
    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        del namespaces
        return ObjectListing(
            (copy.deepcopy(POD), copy.deepcopy(DEPLOYMENT)),
            frozenset({POD_SCOPE, DEPLOYMENT_SCOPE}),
            (ListingFailure(CHAOS_SCOPE, "403 forbidden"),),
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        del namespaces
        return []


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=100)
        return self.now


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'cycles.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _service(factory: sessionmaker[Session]) -> DiagnosisService:
    return DiagnosisService(
        session_factory=factory,
        namespaces=("shop",),
        evidence_namespaces=("chaos-mesh",),
        reader=Cluster(),
        clock=Clock(),
    )


def test_a_run_cycle_is_persisted_once_with_full_bodies(factory: sessionmaker[Session]) -> None:
    result = _service(factory).snapshot_result(run_id="run-1")

    assert result.cycle_id is not None
    with factory() as session:
        cycle = session.scalars(select(SnapshotCycleRow)).one()
        objects = {
            row.object_key: row for row in session.scalars(select(SnapshotCycleObjectRow)).all()
        }
    assert cycle.cycle_id == result.cycle_id
    assert cycle.run_id == "run-1"
    assert cycle.started_at == result.started_at
    assert cycle.started_at <= cycle.observed_at <= cycle.completed_at
    assert cycle.completed_scopes == [["shop", "Deployment"], ["shop", "Pod"]]
    assert cycle.failed_scopes == [
        {"namespace": "chaos-mesh", "kind": "NetworkChaos", "error": "403 forbidden"}
    ]
    assert set(objects) == {"shop/Pod/web-0", "shop/Deployment/web"}
    pod = objects["shop/Pod/web-0"]
    assert pod.body == POD  # full body, status included
    assert (pod.namespace, pod.kind, pod.name, pod.uid) == ("shop", "Pod", "web-0", "uid-a")
    assert pod.evidence_id == f"snapshot:{cycle.cycle_id}:shop/Pod/web-0"


def test_watch_cycles_without_a_run_write_no_snapshot(factory: sessionmaker[Session]) -> None:
    result = _service(factory).snapshot_result()

    assert result.cycle_id is None
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SnapshotCycleRow)) == 0
        assert session.scalar(select(func.count()).select_from(SnapshotCycleObjectRow)) == 0


def test_a_persisted_cycle_cannot_be_changed(factory: sessionmaker[Session]) -> None:
    _service(factory).snapshot_result(run_id="run-1")
    with factory() as session:
        cycle = session.scalars(select(SnapshotCycleRow)).one()
        cycle.completed_at = T0
        with pytest.raises(AuthoritativeEvidenceMutation):
            session.flush()


def test_diagnosis_capture_persists_its_cycle_under_its_run(
    setup: Any,  # noqa: F811
) -> None:
    factory, cluster, clock, incident_id = setup
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    service.snapshot_result()  # a watch cycle before the incident is diagnosed
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id, "MANUAL")

    with factory() as session:
        cycles = session.scalars(select(SnapshotCycleRow)).all()
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        listed = session.scalar(select(func.count()).select_from(SnapshotCycleObjectRow))
    assert [cycle.run_id for cycle in cycles] == [run_id]
    assert listed == len(cluster.objects)


def test_diagnosis_object_history_resolves_to_persisted_rows(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, cluster, clock, incident_id = setup
    seen: list[Mapping[EntityRef, Sequence[ObjectVersion]]] = []
    original = LiveSource.object_history

    def spy(self: LiveSource) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        result = original(self)
        seen.append(result)
        return result

    monkeypatch.setattr(LiveSource, "object_history", spy)
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    service.snapshot_result()
    clock.now = T0 + timedelta(minutes=5)
    cluster.objects[0]["spec"] = {"replicas": 3}  # desired state changes before the capture
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id, "MANUAL")

    with factory() as session:
        journal_ids = {
            f"journal:{version_id}"
            for version_id in session.scalars(select(ObjectVersionRow.version_id))
        }
        snapshot_ids = set(session.scalars(select(SnapshotCycleObjectRow.evidence_id)))
    ids = {
        version.evidence_id for history in seen for items in history.values() for version in items
    }
    assert seen and ids
    assert ids <= journal_ids | snapshot_ids
    assert not any(item.startswith("cluster:") for item in ids)
