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
        self.scope_lists: list[ListingScope] = []
        self.scope_down = False

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        self.lists += 1
        return ObjectListing(
            tuple(self.objects.values()),
            frozenset({PODS}),
            resource_versions={PODS: str(self.version), EVENTS: str(self.version)},
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return []

    def list_scope(self, scope: ListingScope) -> tuple[list[dict[str, Any]], str]:
        """One scope's LIST (contract §15.4): its current items and version."""
        self.scope_lists.append(scope)
        if self.scope_down:
            raise RuntimeError("apiserver down")
        self.version += 1
        items = list(self.objects.values()) if scope == PODS else []
        return items, str(self.version)

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


def test_an_expired_version_is_a_gap_of_its_scope_and_a_relist_of_that_scope_only() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    cursor = client.read("read_changes", None).next_cursor  # a consumer past the first snapshot
    cluster.batches[EVENTS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    tail = since(client, cursor)
    gaps = [
        (g.reason, g.scope.kind if g.scope else None) for g in tail if isinstance(g, wire.GapItem)
    ]
    assert gaps == [("RESOURCE_VERSION_EXPIRED", "Event")]
    begins = [b for b in tail if isinstance(b, wire.SnapshotBeginItem)]
    assert [b.scope.kind if b.scope else None for b in begins] == ["Event"]  # no global snapshot
    assert cluster.lists == 1 and cluster.scope_lists == [EVENTS]
    connector.watch_changes_once()
    assert (PODS, "10") in cluster.watched_from[
        -2:
    ]  # the other scope kept its version and continuity
    assert (EVENTS, "11") in cluster.watched_from  # the relisted scope resumed from its new version


def test_a_scope_relist_restores_the_current_state_and_never_the_lost_interval() -> None:
    from packages.connector.client import StreamedClusterReader
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    reader = StreamedClusterReader(client)
    assert {o["metadata"]["name"] for o in reader.list_objects(["shop"]).objects} == {"a", "b"}
    # while continuity is lost, "ghost" is created and deleted and "b" is deleted: the relist sees neither
    del cluster.objects["b"]
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    listing = reader.list_objects(["shop"])
    assert {o["metadata"]["name"] for o in listing.objects} == {
        "a"
    }  # the scope is the current state
    assert PODS in listing.completed_scopes
    assert not any(
        isinstance(i, wire.ObjectItem) and i.body["metadata"]["name"] == "ghost"
        for i in items(client)
    )


def test_during_a_scope_gap_only_that_scope_is_incomplete_and_the_relist_is_retried() -> None:
    from packages.connector.client import StreamedClusterReader
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, _ = make()
    reader = StreamedClusterReader(client)
    cluster.scope_down = True
    cluster.batches[PODS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    listing = reader.list_objects(["shop"])  # still served: the gap is not global
    assert PODS not in listing.completed_scopes  # no deletion authority for the gapped scope
    cluster.scope_down = False
    assert connector.retry_snapshot_once()  # the scope relist is retried, not a global snapshot
    assert cluster.lists == 1 and cluster.scope_lists == [PODS, PODS]
    assert PODS in reader.list_objects(["shop"]).completed_scopes
    assert not connector.retry_snapshot_once()


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


def test_a_snapshot_with_failed_scopes_keeps_retrying_so_no_scope_stays_unwatched() -> None:
    from packages.rca.live import ListingFailure

    connector, _, cluster, _ = make()
    cluster.batches[PODS] = [RuntimeError("apiserver down")]
    connector.watch_changes_once()  # degraded
    listing = cluster.list_objects

    def half_ready(namespaces: Sequence[str]) -> ObjectListing:
        # the API answers again but not yet for every scope: no version, a failed scope
        return ObjectListing((), frozenset(), (ListingFailure(PODS, "not ready"),))

    cluster.list_objects = half_ready  # type: ignore[method-assign]
    assert connector.retry_snapshot_once()  # retried, but incomplete
    cluster.list_objects = listing  # type: ignore[method-assign]
    assert connector.retry_snapshot_once()  # retried again, now complete
    assert not connector.retry_snapshot_once()
    cluster.batches[PODS] = [[event("MODIFIED", pod("a", "11"), "11")]]
    connector.watch_changes_once()
    assert (PODS, "10") in cluster.watched_from[-2:] or (PODS, "10") in cluster.watched_from


# ---- coverage recording (late-evidence-design.md §4) ----------------------------------------------


def test_a_scope_gap_says_since_when_its_scope_was_continuously_observed() -> None:
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, clock = make()
    cursor = client.read("read_changes", None).next_cursor
    clock.advance(30)
    connector.watch_changes_once()  # the watches end normally at T0+30: continuity proven until then
    clock.advance(270)
    cluster.batches[EVENTS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()  # the resume at T0+300 fails
    (gap,) = [g for g in since(client, cursor) if isinstance(g, wire.GapItem)]
    assert gap.since == T0 + timedelta(seconds=30) and gap.at == T0 + timedelta(seconds=300)


def test_a_change_heartbeat_carries_the_connectors_time_on_a_quiet_stream() -> None:
    connector, client, _, clock = make()
    cursor = client.read("read_changes", None).next_cursor
    clock.advance(5)
    connector.change_heartbeat()
    (beat,) = since(client, cursor)
    assert isinstance(beat, wire.ChangeHeartbeatItem) and beat.observed_at == T0 + timedelta(
        seconds=5
    )


def test_the_mirror_keeps_each_gap_until_it_is_recorded_and_a_heartbeat_is_no_change() -> None:
    from packages.connector.client import StreamedClusterReader
    from packages.connector.watch import ResourceVersionExpired

    connector, client, cluster, clock = make()
    mirror = StreamedClusterReader(client)
    mirror.pending()  # drain the initial snapshot
    clock.advance(5)
    connector.change_heartbeat()
    assert mirror.pending() is False  # a heartbeat carries only the Connector's time
    assert mirror.connector_time() == T0 + timedelta(seconds=5)
    cluster.batches[EVENTS] = [ResourceVersionExpired("410 Gone")]
    connector.watch_changes_once()
    assert mirror.pending() is True
    (gap,) = mirror.gaps()
    assert gap.scope is not None and gap.scope.kind == "Event" and gap.since == T0
    mirror.forget_gaps(1)
    assert mirror.gaps() == ()


def test_the_gaps_a_window_overlaps_are_its_scopes_gaps_and_every_global_one() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from packages.storage.models import Base
    from packages.storage.repositories import ChangeStreamGapRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def at(minutes: float) -> datetime:
        return T0 + timedelta(minutes=minutes)

    with Session(engine) as session:
        gaps = ChangeStreamGapRepository(session)
        gaps.record("RESOURCE_VERSION_EXPIRED", at(2), since=at(1), scope=("shop", "Event"))
        gaps.record("RESOURCE_VERSION_EXPIRED", at(20), since=at(15), scope=("shop", "Event"))
        gaps.record("RESOURCE_VERSION_EXPIRED", at(3), since=at(1), scope=("other", "Event"))
        gaps.record("BACKEND_UNREACHABLE", at(4), since=None, scope=None)  # start unknown
        found = gaps.overlapping(namespaces={"shop"}, starts_at=at(0), ends_at=at(10))
    assert [(g.namespace, g.kind, g.reason) for g in found] == [
        ("shop", "Event", "RESOURCE_VERSION_EXPIRED"),
        (None, None, "BACKEND_UNREACHABLE"),
    ]


def test_the_control_plane_records_the_gaps_it_read_and_keeps_them_when_the_write_fails() -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from apps.control_plane.diagnosis import DiagnosisService
    from packages.storage.models import Base, ChangeStreamGapRow

    class Reader:
        def __init__(self) -> None:
            self.queue = [
                wire.GapItem(
                    seq=1,
                    at=T0,
                    reason="RESOURCE_VERSION_EXPIRED",
                    scope=wire.ScopeWire(namespace="shop", kind="Event"),
                    since=T0 - timedelta(minutes=10),
                )
            ]

        def gaps(self) -> tuple[wire.GapItem, ...]:
            return tuple(self.queue)

        def forget_gaps(self, count: int) -> None:
            del self.queue[:count]

    service = DiagnosisService.__new__(DiagnosisService)
    reader = Reader()
    service.reader = reader  # type: ignore[assignment]
    broken = create_engine("sqlite://")  # no tables: the write fails
    with Session(broken) as session:
        service._record_stream_gaps(session)
    assert len(reader.queue) == 1
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        service._record_stream_gaps(session)
        (row,) = session.scalars(select(ChangeStreamGapRow))
        assert (row.namespace, row.kind, row.reason) == (
            "shop",
            "Event",
            "RESOURCE_VERSION_EXPIRED",
        )
        assert row.since == T0 - timedelta(minutes=10) and row.at == T0
    assert reader.queue == []
