"""The watch-driven change stream (connector contract §15): continuity, gaps and the two kinds of deletion."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.connector import wire
from packages.connector.client import ConnectorClient, in_process_transport
from packages.connector.service import Connector
from packages.rca.live import ListingScope, ObjectListing

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
PODS = ListingScope("shop", "Pod")
EVENTS = ListingScope("shop", "Event")


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


def since(client: ConnectorClient, cursor: str | None) -> list[wire.StreamItem]:
    """What a consumer that already held ``cursor`` reads next: a gap reaches only such a consumer."""
    return client.read("read_changes", cursor).ordered()


def after_snapshot(client: ConnectorClient) -> list[wire.StreamItem]:
    everything = items(client)
    end = max(i for i, item in enumerate(everything) if isinstance(item, wire.ListingStatusItem))
    return everything[end + 1 :]


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


def test_a_watch_event_delivered_twice_is_one_item() -> None:
    connector, client, cluster, _ = make()
    change = pod("a", "11")
    cluster.batches[PODS] = [[event("MODIFIED", change, "11"), event("MODIFIED", change, "11")]]
    connector.watch_changes_once()
    assert [type(i).__name__ for i in after_snapshot(client)] == ["ObjectItem"]


def test_an_expired_version_is_a_gap_then_a_new_snapshot() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    cursor = client.read(
        "read_changes", None
    ).next_cursor  # a consumer that has read the first snapshot
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    tail = since(client, cursor)
    gaps = [i for i in tail if isinstance(i, wire.GapItem)]
    assert [g.reason for g in gaps] == ["RESOURCE_VERSION_EXPIRED"]
    snapshots = [i for i in tail if isinstance(i, wire.SnapshotBeginItem)]
    assert len(snapshots) == 1 and cluster.lists == 2  # the relist after the gap is a new snapshot


def test_a_create_and_delete_inside_the_lost_interval_is_not_reconstructed() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    # while continuity is lost, "ghost" is created and deleted: the relist cannot see it
    cursor = client.read("read_changes", None).next_cursor
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    names = [i.body["metadata"]["name"] for i in items(client) if isinstance(i, wire.ObjectItem)]
    assert "ghost" not in names
    assert not any(
        isinstance(i, wire.ObjectDeletedItem) and "ghost" in i.key for i in items(client)
    )
    # the stream says continuity was lost; it never claims the interval was complete
    assert any(isinstance(i, wire.GapItem) for i in since(client, cursor))


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


def test_the_mirror_says_once_that_changes_arrived() -> None:
    from packages.connector.client import StreamedClusterReader

    connector, client, cluster, _ = make()
    reader = StreamedClusterReader(client)
    assert reader.pending()  # the first snapshot
    assert not reader.pending()  # nothing new since
    cluster.batches[PODS] = [[event("MODIFIED", pod("a", "11"), "11")]]
    connector.watch_changes_once()
    assert reader.pending() and not reader.pending()


def test_the_background_loop_watches_every_scope_and_delivers_a_change_at_once() -> None:
    import threading
    import time

    clock = Clock()
    cluster = FakeWatchCluster()
    connector = Connector(cluster=cluster, watch_namespaces=("shop",), clock=clock)
    client = ConnectorClient(in_process_transport(connector))
    original = cluster.watch

    def slow_watch(scope: ListingScope, version: str) -> Iterator[Any]:
        time.sleep(0.02)  # a real watch blocks; an empty fake would spin
        return original(scope, version)

    cluster.watch = slow_watch  # type: ignore[method-assign,assignment]
    cluster.batches[PODS] = [[event("MODIFIED", pod("a", "11"), "11")]]
    stop = threading.Event()
    loop = threading.Thread(
        target=connector.run, args=(stop,), kwargs={"reconcile_interval": 3600.0}, daemon=True
    )
    loop.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(
                isinstance(i, wire.ObjectItem) and i.body["metadata"]["resourceVersion"] == "11"
                for i in items(client)
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the watched change never reached the stream")
    finally:
        stop.set()
        loop.join(timeout=5)
    assert {PODS, EVENTS} <= {scope for scope, _ in cluster.watched_from}
    assert cluster.lists == 1  # no reconciliation within the interval


def test_the_control_plane_journals_only_when_the_stream_carried_something() -> None:
    import threading

    from apps.control_plane.diagnosis import DiagnosisService

    calls: list[bool] = []

    class Reader:
        def __init__(self) -> None:
            self.answers = [True, False, False, True]

        def pending(self) -> bool:
            return self.answers.pop(0) if self.answers else False

    service = DiagnosisService.__new__(DiagnosisService)
    service.reader = Reader()  # type: ignore[assignment]
    service.snapshot = lambda: calls.append(True) or 0  # type: ignore[method-assign,func-returns-value]
    stop = threading.Event()
    thread = threading.Thread(target=service.follow_changes, args=(stop, 0.001), daemon=True)
    thread.start()
    import time

    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2)
    assert len(calls) == 2  # once per page that carried something, never on a fixed interval


def test_the_journal_keeps_when_the_connector_observed_a_change_without_using_it() -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from packages.storage.models import Base, JournalArrivalRow, ObjectVersionRow
    from packages.storage.repositories import ObjectVersionRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    seen = T0 - timedelta(seconds=14)
    with Session(engine) as session:
        repository = ObjectVersionRepository(session)
        repository.record(pod("a"), T0, connector_observed_at=seen)
        repository.record(pod("b"), T0)  # a change that did not come through a stream
        ids = {r.name: r.version_id for r in session.scalars(select(ObjectVersionRow))}
        arrivals = {
            r.version_id: r.connector_observed_at
            for r in session.scalars(select(JournalArrivalRow))
        }
    assert arrivals[ids["a"]].replace(tzinfo=UTC) == seen
    assert ids["b"] not in arrivals  # the evidence row itself carries no new column


def test_an_api_outage_is_one_gap_and_one_snapshot_on_recovery_not_a_storm() -> None:
    connector, client, cluster, _ = make()
    cursor = client.read("read_changes", None).next_cursor
    cluster.batches[PODS] = [RuntimeError("apiserver down"), RuntimeError("apiserver down")]
    cluster.batches[EVENTS] = [RuntimeError("apiserver down")]
    connector.watch_changes_once()  # the first failing watch
    connector.watch_changes_once()  # and another one, while still down
    for scope in (PODS, EVENTS):
        connector.watch_scope_once(scope)
    gaps = [i for i in since(client, cursor) if isinstance(i, wire.GapItem)]
    assert [g.reason for g in gaps] == ["BACKEND_UNREACHABLE"]  # one gap for the outage
    assert cluster.lists == 1  # no watch thread hammers the API with its own snapshot
    assert connector.retry_snapshot_once()  # the loop retries; the API is back
    assert cluster.lists == 2
    tail = since(client, cursor)
    assert sum(isinstance(i, wire.SnapshotBeginItem) for i in tail) == 1
    assert not connector.retry_snapshot_once()  # recovered: nothing left to retry


def test_an_expiry_whose_relist_fails_is_retried_by_the_loop() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, _, cluster, _ = make()
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    listing = cluster.list_objects

    def down(namespaces: Sequence[str]) -> ObjectListing:
        raise RuntimeError("apiserver down")

    cluster.list_objects = down  # type: ignore[method-assign]
    connector.watch_changes_once()
    cluster.list_objects = listing  # type: ignore[method-assign]
    assert connector.retry_snapshot_once() and not connector.retry_snapshot_once()
