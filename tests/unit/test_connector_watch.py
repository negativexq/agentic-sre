"""The watch-driven change stream (connector contract §15): continuity, gaps and the two kinds of deletion.

Written before the implementation; each test is a strict expected failure until the watch path exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.connector import wire
from packages.connector.client import ConnectorClient, in_process_transport
from packages.connector.service import Connector
from packages.rca.live import ListingScope, ObjectListing

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
PODS = ListingScope("shop", "Pod")
EVENTS = ListingScope("shop", "Event")
PENDING = pytest.mark.xfail(strict=True, reason="watch path not implemented yet (contract §15)")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def pod(name: str, version: str = "1", uid: str | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "shop",
            "uid": uid or f"uid-{name}",
            "resourceVersion": version,
        },
        "status": {"phase": "Running", "version": version},
    }


class FakeWatchCluster:
    """A cluster whose LIST returns versions and whose WATCH replays scripted batches.

    Each ``watch`` call takes the next batch for its scope: a list of watch events (the watch then ends,
    as the server closes it) or an exception to raise.
    """

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {"a": pod("a"), "b": pod("b")}
        self.version = 10
        self.batches: dict[ListingScope, list[Any]] = {PODS: [], EVENTS: []}
        self.watched_from: list[tuple[ListingScope, str]] = []
        self.lists = 0

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        self.lists += 1
        return ObjectListing(
            tuple(self.objects.values()),
            frozenset({PODS}),
            resource_versions={PODS: str(self.version), EVENTS: str(self.version)},
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return []

    def watch(self, scope: ListingScope, resource_version: str) -> Iterator[Any]:
        self.watched_from.append((scope, resource_version))
        queue = self.batches.get(scope) or []
        if not queue:
            return iter(())
        batch = queue.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        return iter(batch)


def event(kind: Any, body: dict[str, Any], version: str) -> Any:
    from packages.connector.watch import WatchEvent

    return WatchEvent(type=kind, body=body, resource_version=version)


def make() -> tuple[Connector, ConnectorClient, FakeWatchCluster, Clock]:
    clock = Clock()
    cluster = FakeWatchCluster()
    connector = Connector(cluster=cluster, watch_namespaces=("shop",), clock=clock)
    connector.poll_changes_once()  # LIST: the snapshot and each scope's version
    return connector, ConnectorClient(in_process_transport(connector)), cluster, clock


def items(client: ConnectorClient) -> list[wire.StreamItem]:
    return client.read("read_changes", None).ordered()


def after_snapshot(client: ConnectorClient) -> list[wire.StreamItem]:
    everything = items(client)
    end = max(i for i, item in enumerate(everything) if isinstance(item, wire.ListingStatusItem))
    return everything[end + 1 :]


@PENDING
def test_a_watch_event_is_emitted_and_the_watch_resumes_from_its_version_without_a_gap() -> None:
    connector, client, cluster, _ = make()
    cluster.batches[PODS] = [[event("MODIFIED", pod("a", "11"), "11")], []]
    connector.watch_changes_once()  # delivers the change, then the server closes the watch
    connector.watch_changes_once()  # resumes
    tail = after_snapshot(client)
    assert [type(i).__name__ for i in tail] == ["ObjectItem"]
    assert (PODS, "11") in cluster.watched_from  # resumed from the version it last saw
    assert not any(isinstance(i, wire.GapItem) for i in items(client))
    assert cluster.lists == 1  # a resumed watch never relists


@PENDING
def test_a_watch_event_delivered_twice_is_one_item() -> None:
    connector, client, cluster, _ = make()
    change = pod("a", "11")
    cluster.batches[PODS] = [[event("MODIFIED", change, "11"), event("MODIFIED", change, "11")]]
    connector.watch_changes_once()
    assert [type(i).__name__ for i in after_snapshot(client)] == ["ObjectItem"]


@PENDING
def test_an_expired_version_is_a_gap_then_a_new_snapshot() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    tail = items(client)
    gaps = [i for i in tail if isinstance(i, wire.GapItem)]
    assert [g.reason for g in gaps] == ["RESOURCE_VERSION_EXPIRED"]
    snapshots = [i for i in tail if isinstance(i, wire.SnapshotBeginItem)]
    assert len(snapshots) == 2 and cluster.lists == 2  # the first LIST and the relist after the gap


@PENDING
def test_a_create_and_delete_inside_the_lost_interval_is_not_reconstructed() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    # while continuity is lost, "ghost" is created and deleted: the relist cannot see it
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    names = [i.body["metadata"]["name"] for i in items(client) if isinstance(i, wire.ObjectItem)]
    assert "ghost" not in names
    assert not any(
        isinstance(i, wire.ObjectDeletedItem) and "ghost" in i.key for i in items(client)
    )
    # the stream says continuity was lost; it never claims the interval was complete
    assert any(isinstance(i, wire.GapItem) for i in items(client))


@PENDING
def test_a_watch_delete_is_an_observed_deletion_and_a_listing_absence_an_inferred_one() -> None:
    connector, client, cluster, clock = make()
    cluster.batches[PODS] = [[event("DELETED", pod("a", "12"), "12")]]
    connector.watch_changes_once()
    observed = [i for i in items(client) if isinstance(i, wire.ObjectDeletedItem)]
    assert [(d.key.endswith("/a"), d.source) for d in observed] == [(True, "watch")]
    # a reconciliation that no longer lists "b" infers its deletion
    del cluster.objects["a"], cluster.objects["b"]
    clock.advance(600)
    connector.poll_changes_once()
    inferred = [
        i for i in items(client) if isinstance(i, wire.ObjectDeletedItem) and i.key.endswith("/b")
    ]
    assert [d.source for d in inferred] == ["listing"]


@PENDING
def test_a_watch_never_emits_a_denied_kind() -> None:
    connector, client, cluster, _ = make()
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "s", "namespace": "shop", "uid": "uid-s", "resourceVersion": "11"},
        "data": {"password": "c2VjcmV0"},
    }
    cluster.batches[PODS] = [[event("ADDED", secret, "11")]]
    connector.watch_changes_once()
    assert after_snapshot(client) == []
