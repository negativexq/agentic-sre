"""Connector streams (contract §10): resume, duplicates, restart, expiry, snapshots, coverage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.control_plane.connector_intake import AlertStreamConsumer
from packages.connector import wire
from packages.connector.client import (
    ConnectorClient,
    ConnectorUnavailable,
    StreamedClusterReader,
    in_process_transport,
)
from packages.connector.service import Connector
from packages.rca.alert_coverage import ALERT_COVERAGE_SOURCE, AlertCoverageConfig
from packages.rca.live import ListingFailure, ListingScope, ObjectListing
from packages.storage.models import Base
from packages.storage.repositories import AlertCoverageRepository

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def pod(name: str, version: str = "1", namespace: str = "shop") -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "resourceVersion": version},
    }


class FakeCluster:
    def __init__(self) -> None:
        self.objects: list[dict[str, Any]] = [pod("a"), pod("b")]
        self.events: list[dict[str, Any]] = []
        self.failed: list[ListingFailure] = []
        self.down = False
        self.listings = 0

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        self.listings += 1
        if self.down:
            raise TimeoutError("api server unreachable")
        completed = frozenset({ListingScope("shop", "Pod")} - {f.scope for f in self.failed})
        return ObjectListing(tuple(self.objects), completed, tuple(self.failed))

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return list(self.events)


class FakeAlerts:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []
        self.down = False

    def list_alerts(self) -> list[dict[str, Any]]:
        if self.down:
            raise ConnectionError("alertmanager down")
        return list(self.alerts)


def firing(name: str, fingerprint: str, starts: datetime = T0) -> dict[str, Any]:
    return {
        "labels": {"alertname": name, "service": "checkout", "namespace": "shop"},
        "annotations": {},
        "startsAt": starts.isoformat(),
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
        "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
    }


def make(
    *, clock: Clock | None = None, buffer: int = 1000
) -> tuple[Connector, ConnectorClient, FakeCluster, FakeAlerts, Clock]:
    clock = clock or Clock()
    cluster, alerts = FakeCluster(), FakeAlerts()
    connector = Connector(
        cluster=cluster,
        alert_source=alerts,
        accept_webhook=True,
        watch_namespaces=("shop",),
        clock=clock,
        alerts_buffer_len=buffer,
        changes_buffer_len=buffer,
    )
    return connector, ConnectorClient(in_process_transport(connector)), cluster, alerts, clock


def kinds(page: Any) -> list[str]:
    return [item.kind for item in page.ordered()]


# ---- changes stream and the mirror ---------------------------------------------------------


def test_a_fresh_reader_gets_a_complete_snapshot_then_only_deltas() -> None:
    connector, client, cluster, _, clock = make()
    reader = StreamedClusterReader(client)
    first = reader.list_objects(["shop"])
    assert [o["metadata"]["name"] for o in first.objects] == ["a", "b"]
    assert first.completed_scopes == frozenset({ListingScope("shop", "Pod")})
    cluster.objects = [pod("a", "2"), pod("b"), pod("c")]
    clock.advance(15)
    connector.poll_changes_once()
    page = client.read("read_changes", reader._cursor)  # noqa: SLF001
    assert kinds(page) == ["object", "object", "listing_status"]  # a changed, c new; b is quiet
    second = reader.list_objects(["shop"])
    assert [o["metadata"]["resourceVersion"] for o in second.objects] == ["2", "1", "1"]


def test_a_deletion_is_inferred_only_for_a_completely_listed_scope() -> None:
    connector, client, cluster, _, clock = make()
    reader = StreamedClusterReader(client)
    reader.list_objects(["shop"])
    cluster.objects = [pod("a")]
    cluster.failed = [ListingFailure(ListingScope("shop", "Pod"), "Forbidden")]
    clock.advance(15)
    connector.poll_changes_once()  # the Pod scope failed: absence proves nothing
    listing = reader.list_objects(["shop"])
    assert {o["metadata"]["name"] for o in listing.objects} == {"a", "b"}
    assert listing.failed_scopes == tuple(cluster.failed)
    cluster.failed = []
    clock.advance(15)
    connector.poll_changes_once()  # complete now: b is gone
    assert {o["metadata"]["name"] for o in reader.list_objects(["shop"]).objects} == {"a"}


def test_a_backend_gap_blocks_the_listing_instead_of_serving_what_was_last_known() -> None:
    connector, client, cluster, _, clock = make()
    reader = StreamedClusterReader(client)
    reader.list_objects(["shop"])
    cluster.down = True
    clock.advance(15)
    connector.poll_changes_once()
    with pytest.raises(ConnectorUnavailable):
        reader.list_objects(["shop"])
    cluster.down = False
    clock.advance(15)
    connector.poll_changes_once()
    assert len(reader.list_objects(["shop"]).objects) == 2


def test_a_restarted_connector_declares_the_gap_and_serves_a_new_snapshot() -> None:
    connector, client, cluster, _, clock = make()
    reader = StreamedClusterReader(client)
    reader.list_objects(["shop"])
    old_cursor = reader._cursor  # noqa: SLF001
    restarted = Connector(
        cluster=cluster, watch_namespaces=("shop",), clock=clock, changes_buffer_len=1000
    )
    fresh = ConnectorClient(in_process_transport(restarted))
    page = fresh.read("read_changes", old_cursor)
    assert [g.reason for g in page.gaps] == ["CONNECTOR_RESTART"]
    assert page.items[0].kind == "snapshot_begin"
    assert page.items[-1].kind == "listing_status"


def test_an_expired_cursor_is_a_gap_and_restarts_from_the_latest_snapshot() -> None:
    connector, client, cluster, _, clock = make(buffer=12)
    page = client.read("read_changes", None)
    stale = page.next_cursor
    for version in range(2, 10):
        cluster.objects = [pod("a", str(version)), pod("b")]
        clock.advance(15)
        connector.poll_changes_once()
    again = client.read("read_changes", stale)
    assert [g.reason for g in again.gaps] == ["BUFFER_EXPIRED"]
    assert again.items[0].kind == "snapshot_begin"


def test_a_snapshot_without_its_end_proves_nothing() -> None:
    reader = StreamedClusterReader(ConnectorClient(lambda _: b""))
    reader._apply(wire.SnapshotBeginItem(seq=1, observed_at=T0))  # noqa: SLF001
    reader._apply(wire.ObjectItem(seq=2, observed_at=T0, body=pod("a")))  # noqa: SLF001
    with pytest.raises(ConnectorUnavailable):
        reader._current()  # noqa: SLF001 - drain fails on the dead transport first
    assert reader._staging is not None and not reader._objects  # noqa: SLF001


def test_denied_kinds_never_enter_the_change_stream() -> None:
    connector, client, cluster, _, _ = make()
    cluster.objects = [pod("a"), {"kind": "Secret", "metadata": {"name": "s", "namespace": "shop"}}]
    page = client.read("read_changes", None)
    assert [i.body["kind"] for i in page.items if isinstance(i, wire.ObjectItem)] == ["Pod"]


def test_reading_twice_from_the_same_cursor_returns_the_same_items() -> None:
    _, client, _, _, _ = make()
    first = client.read("read_changes", None)
    assert client.read("read_changes", None).items == first.items


# ---- alert stream and intake ---------------------------------------------------------------


def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_a_poll_yields_the_occurrence_and_a_heartbeat_then_the_resolution() -> None:
    connector, client, _, alerts, clock = make()
    alerts.alerts = [firing("HighLatency", "f1")]
    connector.poll_alerts_once()
    page = client.read("read_alerts", None)
    assert kinds(page) == ["alert", "heartbeat"]
    assert page.items[0].alert["status"] == "firing"  # type: ignore[attr-defined]
    alerts.alerts = []
    clock.advance(60)
    connector.poll_alerts_once()
    later = client.read("read_alerts", page.next_cursor)
    resolved = [i for i in later.items if isinstance(i, wire.AlertItem)]
    assert [a.alert["status"] for a in resolved] == ["resolved"]


def test_a_fingerprint_that_resolved_and_fired_again_between_two_polls_ends_its_old_occurrence() -> (
    None
):
    from datetime import timedelta

    connector, client, _, alerts, clock = make()
    alerts.alerts = [firing("KafkaConsumerLag", "f1")]
    connector.poll_alerts_once()
    cursor = client.read("read_alerts", None).next_cursor
    clock.advance(60)
    # Alertmanager holds one alert per fingerprint: a new start means the earlier occurrence ended
    restart = T0 + timedelta(seconds=40)
    alerts.alerts = [firing("KafkaConsumerLag", "f1", starts=restart)]
    connector.poll_alerts_once()
    later = [
        i.alert for i in client.read("read_alerts", cursor).items if isinstance(i, wire.AlertItem)
    ]
    assert [(a["status"], a["startsAt"]) for a in later] == [
        ("resolved", T0.isoformat()),
        ("firing", restart.isoformat()),
    ]
    assert later[0]["endsAt"] == restart.isoformat()  # ended by the time the new occurrence began


def test_the_incident_of_an_occurrence_replaced_between_polls_does_not_stay_open() -> None:
    from datetime import timedelta

    from packages.storage.models import IncidentRow

    connector, client, _, alerts, clock = make()
    factory = session_factory()
    consumer = AlertStreamConsumer(client, factory)
    alerts.alerts = [firing("KafkaConsumerLag", "f1")]
    connector.poll_alerts_once()
    consumer.step()
    clock.advance(60)
    alerts.alerts = [firing("KafkaConsumerLag", "f1", starts=T0 + timedelta(seconds=40))]
    connector.poll_alerts_once()
    consumer.step()
    with factory() as session:
        statuses = sorted(row.status for row in session.query(IncidentRow))
    assert statuses == ["OPEN", "RESOLVED"]  # the earlier occurrence's incident is closed


def test_only_a_change_of_an_incident_starts_a_diagnosis_and_a_replay_starts_none() -> None:
    connector, client, _, alerts, clock = make()
    factory = session_factory()
    created: list[list[Any]] = []
    resolved: list[list[Any]] = []
    consumer = AlertStreamConsumer(
        client, factory, on_incidents=created.append, on_resolved=resolved.append
    )
    alerts.alerts = [firing("HighLatency", "f1")]
    connector.poll_alerts_once()
    consumer.step()
    (incident,) = created[0]
    clock.advance(60)
    connector.poll_alerts_once()  # the same occurrence, still firing: nothing new on the stream
    consumer.step()
    replay = AlertStreamConsumer(  # a restarted control plane reads the buffer again
        client, factory, on_incidents=created.append, on_resolved=resolved.append
    )
    replay.step()
    assert created == [[incident]] and resolved == []
    alerts.alerts = []
    clock.advance(60)
    connector.poll_alerts_once()
    consumer.step()
    assert resolved == [[incident]] and created == [[incident]]


def test_a_replaced_occurrence_reports_its_incident_resolved_and_the_new_one_created() -> None:
    from datetime import timedelta

    connector, client, _, alerts, clock = make()
    factory = session_factory()
    created: list[list[Any]] = []
    resolved: list[list[Any]] = []
    consumer = AlertStreamConsumer(
        client, factory, on_incidents=created.append, on_resolved=resolved.append
    )
    alerts.alerts = [firing("KafkaConsumerLag", "f1")]
    connector.poll_alerts_once()
    consumer.step()
    clock.advance(60)
    alerts.alerts = [firing("KafkaConsumerLag", "f1", starts=T0 + timedelta(seconds=40))]
    connector.poll_alerts_once()
    consumer.step()
    first, second = created[0][0], created[1][0]
    assert first != second and resolved == [[first]]


def test_a_webhook_received_locally_reaches_the_same_stream() -> None:
    connector, client, _, _, _ = make()
    delivery = {
        "receiver": "sre",
        "status": "firing",
        "alerts": [{**firing("Burst", "f2"), "status": "firing"}],
    }
    assert connector.receive_webhook(delivery) == 1
    (item,) = client.read("read_alerts", None).items
    assert isinstance(item, wire.AlertItem) and item.origin == "webhook"


def test_intake_is_idempotent_across_duplicate_delivery_and_replay() -> None:
    connector, client, _, alerts, _ = make()
    alerts.alerts = [firing("HighLatency", "f1")]
    connector.poll_alerts_once()
    factory = session_factory()
    consumer = AlertStreamConsumer(client, factory)
    assert consumer.step() == 2
    replay = AlertStreamConsumer(client, factory)  # a restarted control plane: cursor is lost
    replay.step()
    from packages.storage.models import IncidentRow

    with factory() as session:
        assert session.query(IncidentRow).count() == 1


def test_heartbeats_drive_coverage_and_a_replay_cannot_move_it_backwards() -> None:
    connector, client, _, alerts, clock = make()
    factory = session_factory()
    config = AlertCoverageConfig(poll_interval=timedelta(seconds=60), max_gap_polls=2)
    for _ in range(3):
        connector.poll_alerts_once()
        clock.advance(60)
    consumer = AlertStreamConsumer(client, factory, config=config, clock=clock)
    consumer.step()
    with factory() as session:
        segment = AlertCoverageRepository(session).open_segment(ALERT_COVERAGE_SOURCE)
        assert segment is not None
        last = segment.last_success_at
    AlertStreamConsumer(client, factory, config=config, clock=clock).step()  # replay from None
    with factory() as session:
        segment = AlertCoverageRepository(session).open_segment(ALERT_COVERAGE_SOURCE)
        assert segment is not None and segment.last_success_at == last


def test_an_unreachable_alertmanager_closes_the_segment_and_a_gap_is_recorded() -> None:
    connector, client, _, alerts, clock = make()
    factory = session_factory()
    config = AlertCoverageConfig()
    connector.poll_alerts_once()
    consumer = AlertStreamConsumer(client, factory, config=config, clock=clock)
    consumer.step()
    alerts.down = True
    clock.advance(60)
    connector.poll_alerts_once()
    consumer.step()
    with factory() as session:
        assert AlertCoverageRepository(session).open_segment(ALERT_COVERAGE_SOURCE) is None


def test_an_unreachable_connector_closes_the_open_segment_once() -> None:
    connector, client, _, _, clock = make()
    factory = session_factory()
    connector.poll_alerts_once()
    AlertStreamConsumer(client, factory, clock=clock).step()

    def down(_: bytes) -> bytes:
        raise ConnectionError("gone")

    lost = AlertStreamConsumer(ConnectorClient(down), factory, clock=clock)
    lost.step()
    lost.step()
    with factory() as session:
        assert AlertCoverageRepository(session).open_segment(ALERT_COVERAGE_SOURCE) is None
        from packages.storage.models import AlertCoveragePollRow

        failures = session.query(AlertCoveragePollRow).filter_by(success=False).count()
    assert failures == 1


def test_the_page_limit_and_the_cursor_shape_are_enforced() -> None:
    connector, _, _, _, _ = make()
    over = wire.dumps(
        wire.Request(
            version=wire.WIRE_VERSION, op="read_alerts", args={"cursor": None, "limit": 501}
        )
    )
    assert not wire.loads_response(connector.handle(over)).ok
    bad = wire.dumps(
        wire.Request(version=wire.WIRE_VERSION, op="read_alerts", args={"cursor": "nonsense"})
    )
    assert not wire.loads_response(connector.handle(bad)).ok


def test_streams_are_reported_as_capabilities_only_when_configured() -> None:
    assert Connector().capabilities() == []
    connector, client, *_ = make()
    assert {"alerts", "changes"} <= set(client.capabilities())


def test_a_refiring_within_the_quiet_interval_asks_for_a_refired_revision_not_a_new_incident() -> (
    None
):
    from datetime import timedelta

    connector, client, _, alerts, clock = make()
    factory = session_factory()
    new: list[list[Any]] = []
    refired: list[list[Any]] = []
    consumer = AlertStreamConsumer(
        client,
        factory,
        on_incidents=new.append,
        on_refired=refired.append,
        quiet=timedelta(seconds=30),
    )
    alerts.alerts = [firing("HighLatency", "f1")]
    connector.poll_alerts_once()
    consumer.step()
    alerts.alerts = []
    clock.advance(60)
    connector.poll_alerts_once()  # the occurrence resolves
    consumer.step()
    clock.advance(6)
    alerts.alerts = [firing("HighLatency", "f1", starts=T0 + timedelta(seconds=66))]
    connector.poll_alerts_once()  # and fires again six seconds later
    before = len(new)
    consumer.step()
    from packages.storage.models import IncidentRow

    with factory() as session:
        assert session.query(IncidentRow).count() == 1
    first = new[0][0]
    assert refired == [[first]]  # one refired revision, for the incident that already existed
    assert all(first not in batch for batch in new[before:])  # and no INITIAL for the re-firing
