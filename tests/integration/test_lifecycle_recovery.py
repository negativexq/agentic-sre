"""M19-2.9a: lifecycle persistence failures are recovered, never silently lost.

Faults are injected into the ledger and index writes. A failed transition
keeps the instance's STATUS_SNAPSHOT checkpoint in place, so the next cycle
drafts it again; a missing DELETED or index deletion mark is completed from
the persisted journal tombstone, with that tombstone's own time.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_pod_lifecycle_collection import T0, Clock, Cluster, _pod, _rows, _service, factory

from packages.storage.models import EntityInstanceRow
from packages.storage.repositories import (
    EntityInstanceRepository,
    LifecycleRepository,
    ObjectVersionRepository,
)

__all__ = ["factory"]  # pytest fixture re-exported for this module


def _fail(
    monkeypatch: pytest.MonkeyPatch,
    target: type[Any],
    method: str,
    when: Callable[[dict[str, Any]], bool],
    times: int = 1,
) -> None:
    """Make ``target.method`` raise for the next ``times`` calls matching ``when``."""
    real = getattr(target, method)
    budget = [times]

    def faulty(self: Any, **values: Any) -> Any:
        if budget[0] > 0 and when(values):
            budget[0] -= 1
            raise RuntimeError("injected write failure")
        return real(self, **values)

    monkeypatch.setattr(target, method, faulty)


def _oom(body: dict[str, Any], finished: str = "2026-09-25T12:00:10Z") -> dict[str, Any]:
    body = copy.deepcopy(body)
    body["status"]["containerStatuses"][0].update(
        restartCount=1,
        state={"running": {"startedAt": "2026-09-25T12:00:12Z"}},
        lastState={
            "terminated": {"reason": "OOMKilled", "finishedAt": finished, "containerID": "c1"}
        },
    )
    return body


def _types_at(factory: sessionmaker[Session], uid: str) -> list[tuple[str, float]]:
    return [(kind, at) for kind, at, _ in _rows(factory, uid)]


def test_failed_ready_transition_holds_the_checkpoint_and_recovers(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    _fail(monkeypatch, LifecycleRepository, "append", lambda v: v["type"] == "READY_FALSE")
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = [_pod("web-a", "uid-a", ready="False", since="2026-09-25T12:00:10Z")]
    failed = service.snapshot_result()
    # Neither the transition nor a snapshot past it was recorded.
    assert failed.lifecycle_write_failures == 1
    assert _types_at(factory, "uid-a") == [("OBSERVED", 0), ("STATUS_SNAPSHOT", 0)]

    clock.now = T0 + timedelta(seconds=30)
    assert service.snapshot_result().lifecycle_write_failures == 0
    assert _types_at(factory, "uid-a")[2:] == [("READY_FALSE", 30), ("STATUS_SNAPSHOT", 30)]
    with factory() as session:
        (ready,) = [
            r
            for r in LifecycleRepository(session).list_for("sre-demo", "Pod", "uid-a")
            if r.type == "READY_FALSE"
        ]
    assert ready.source_at == T0 + timedelta(seconds=10)  # the source time survives the retry
    assert service.lifecycle_write_failures == 1


def test_a_failed_container_transition_recovers_without_duplicating_its_sibling(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    # One cycle drafts READY_FALSE, OOM_KILLED and CONTAINER_STARTED; only OOM fails.
    _fail(monkeypatch, LifecycleRepository, "append", lambda v: v["type"] == "OOM_KILLED")
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = [_oom(_pod("web-a", "uid-a", ready="False", since="2026-09-25T12:00:11Z"))]
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=30)
    service.snapshot_result()

    assert _types_at(factory, "uid-a")[2:] == [
        ("READY_FALSE", 15),
        ("CONTAINER_STARTED", 15),
        ("OOM_KILLED", 30),
        ("STATUS_SNAPSHOT", 30),
    ]


def test_a_failed_snapshot_is_written_next_cycle_without_repeating_transitions(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    _fail(monkeypatch, LifecycleRepository, "append", lambda v: v["type"] == "STATUS_SNAPSHOT")
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = [_pod("web-a", "uid-a", ready="False", since="2026-09-25T12:00:10Z")]
    service.snapshot_result()
    clock.now = T0 + timedelta(seconds=30)
    service.snapshot_result()

    assert _types_at(factory, "uid-a")[2:] == [("READY_FALSE", 15), ("STATUS_SNAPSHOT", 30)]


def test_a_failed_deletion_is_completed_from_the_journal_tombstone(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    _fail(monkeypatch, LifecycleRepository, "append", lambda v: v["type"] == "DELETED")
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = []
    assert service.snapshot_result().lifecycle_write_failures == 1
    assert "DELETED" not in [kind for kind, _ in _types_at(factory, "uid-a")]

    clock.now = T0 + timedelta(seconds=30)
    recovered = service.snapshot_result()
    assert recovered.lifecycle_repairs >= 1
    # The tombstone's own time, not the time of the repair.
    assert _rows(factory, "uid-a")[-1] == ("DELETED", 15, "journal-tombstone")


def test_a_failed_index_deletion_mark_is_repaired_from_the_tombstone(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    _fail(monkeypatch, EntityInstanceRepository, "upsert", lambda v: v.get("deleted", False))
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = []
    service.snapshot_result()

    def marked() -> Any:
        with factory() as session:
            row = session.scalar(select(EntityInstanceRow).where(EntityInstanceRow.uid == "uid-a"))
            assert row is not None
            return row.deleted_observed_at

    assert marked() is None
    clock.now = T0 + timedelta(seconds=30)
    service.snapshot_result()
    assert marked() == T0 + timedelta(seconds=15)


def test_absence_without_a_tombstone_never_becomes_a_deletion(
    factory: sessionmaker[Session],
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()

    cluster.pod_listing_failed = True
    for seconds in (15, 30, 45):
        clock.now = T0 + timedelta(seconds=seconds)
        assert service.snapshot_result().lifecycle_repairs == 0
    assert [kind for kind, _ in _types_at(factory, "uid-a")] == ["OBSERVED", "STATUS_SNAPSHOT"]


def test_a_restarted_collector_completes_an_old_tombstone(factory: sessionmaker[Session]) -> None:
    # A previous process journaled the tombstone and died before any lifecycle write.
    with factory() as session:
        journal = ObjectVersionRepository(session)
        journal.record(_pod("web-a", "uid-a"), T0)
        journal.tombstone("sre-demo/Pod/web-a", T0 + timedelta(seconds=15))

    cluster, clock = Cluster(), Clock()
    clock.now = T0 + timedelta(minutes=5)
    _service(factory, cluster, clock).snapshot_result()  # fresh process

    assert _rows(factory, "uid-a") == [("DELETED", 15, "journal-tombstone")]
    with factory() as session:
        row = session.scalar(select(EntityInstanceRow).where(EntityInstanceRow.uid == "uid-a"))
    assert row is not None and row.deleted_observed_at == T0 + timedelta(seconds=15)


def test_reconciliation_is_idempotent(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, clock = Cluster(), Clock()
    service = _service(factory, cluster, clock)
    cluster.pods = [_pod("web-a", "uid-a")]
    service.snapshot_result()
    _fail(monkeypatch, LifecycleRepository, "append", lambda v: v["type"] == "DELETED")
    clock.now = T0 + timedelta(seconds=15)
    cluster.pods = []
    service.snapshot_result()

    repairs = []
    for seconds in (30, 45, 60):
        clock.now = T0 + timedelta(seconds=seconds)
        repairs.append(service.snapshot_result().lifecycle_repairs)
    _service(factory, cluster, clock).snapshot_result()  # and after a restart

    assert repairs[1:] == [0, 0]
    assert [kind for kind, _ in _types_at(factory, "uid-a")].count("DELETED") == 1
