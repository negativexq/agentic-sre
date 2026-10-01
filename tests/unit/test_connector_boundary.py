"""connector.v1: the readers keep their protocols, and every call crosses encoded bytes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.connector import wire
from packages.connector.client import (
    ConnectorClient,
    ConnectorClusterReader,
    ConnectorLokiReader,
    ConnectorPrometheusReader,
    ConnectorReadError,
    ConnectorTempoReader,
    ConnectorUnavailable,
    cluster_reader,
    in_process_transport,
    provider_readers,
)
from packages.connector.service import AuditEntry, Connector
from packages.rca.investigation.tempo import (
    TempoSearchCompleteness,
    TempoSearchDiagnostics,
    TempoTraceBatch,
)
from packages.rca.live import ListingFailure, ListingScope, ObjectListing
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.provider_adapter import ProviderReaders

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
POD = EntityRef(kind="Pod", name="checkout-1", namespace="shop")
QUERY = InvestigationQuery(start=T0, end=T0 + timedelta(minutes=5), limit=16)


class FakeCluster:
    def __init__(self) -> None:
        self.objects: tuple[dict[str, Any], ...] = (
            {"kind": "Pod", "metadata": {"name": "checkout-1", "namespace": "shop"}},
            {"kind": "Secret", "metadata": {"name": "token", "namespace": "shop"}},
        )
        self.fail = False

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        if self.fail:
            raise TimeoutError("api server unreachable")
        return ObjectListing(
            self.objects,
            frozenset({ListingScope("shop", "Pod"), ListingScope("shop", "Secret")}),
            (ListingFailure(ListingScope("shop", "Deployment"), "Forbidden: no access"),),
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return [{"kind": "Event", "reason": "BackOff", "count": 3}]


class FakeProm:
    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        return (
            ResourcePressure(
                pod=target,
                container="app",
                resource="memory",
                baseline=0.31,
                peak=0.97,
                at=T0,
                evidence_id="prom:1",
            ),
        )

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        return (
            TrafficObservation(
                entity=target, metric="rps", at=T0, value=12.5, evidence_id="prom:2"
            ),
        )


class FakeLoki:
    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        return [
            LogRecord(
                service=services[0],
                at=starts_at,
                severity="error",
                message="boom",
                evidence_id="l:1",
            )
        ]


class FakeTempo:
    def __init__(self, batch: bool) -> None:
        self.batch = batch

    def query(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
        span = TraceSpanObservation(
            trace_id="t1",
            span_id="s1",
            service="checkout",
            start_at=T0,
            end_at=T0 + timedelta(seconds=1),
            duration_raw=1.0,
            evidence_id="tempo:1",
        )
        if not self.batch:
            return (span,)
        return TempoTraceBatch(
            (span,),
            TempoSearchDiagnostics(
                completeness=TempoSearchCompleteness.TRUNCATED,
                candidate_trace_ids=("t1", "t2"),
                search_limit_reached=True,
                inspected_traces=2,
                inspected_bytes=None,
                completed_jobs=1,
                total_jobs=2,
                fetched_trace_ids=("t1",),
                missing_trace_ids=("t2",),
            ),
        )


def build(
    *, batch: bool = False, audit: list[AuditEntry] | None = None
) -> tuple[Connector, ConnectorClient, FakeCluster]:
    cluster = FakeCluster()
    connector = Connector(
        cluster=cluster,
        providers=ProviderReaders(prometheus=FakeProm(), loki=FakeLoki(), tempo=FakeTempo(batch)),
        audit=audit.append if audit is not None else None,
    )
    return connector, ConnectorClient(in_process_transport(connector)), cluster


def raw(connector: Connector, **fields: Any) -> wire.Response:
    payload = json.dumps(fields).encode()
    return wire.loads_response(connector.handle(payload))


def test_results_come_back_equal_to_what_the_backends_returned() -> None:
    _, client, _ = build()
    assert ConnectorPrometheusReader(client).query_resource_pressure(
        POD, QUERY
    ) == FakeProm().query_resource_pressure(POD, QUERY)
    assert ConnectorPrometheusReader(client).query_traffic(POD, QUERY) == FakeProm().query_traffic(
        POD, QUERY
    )
    logs = ConnectorLokiReader(client).error_logs(["checkout"], T0, T0 + timedelta(minutes=1))
    assert logs == FakeLoki().error_logs(["checkout"], T0, T0)
    assert ConnectorTempoReader(client).query(POD, QUERY) == FakeTempo(False).query(POD, QUERY)


def test_a_tempo_batch_keeps_its_diagnostics() -> None:
    _, client, _ = build(batch=True)
    assert ConnectorTempoReader(client).query(POD, QUERY) == FakeTempo(True).query(POD, QUERY)


def test_the_object_listing_keeps_its_completeness_and_failures_and_drops_secrets() -> None:
    _, client, cluster = build()
    listing = ConnectorClusterReader(client).list_objects(["shop"])
    assert [o["kind"] for o in listing.objects] == ["Pod"]
    assert listing.completed_scopes == cluster.list_objects([]).completed_scopes
    assert listing.failed_scopes == cluster.list_objects([]).failed_scopes
    assert ConnectorClusterReader(client).list_events(["shop"]) == cluster.list_events([])


def test_the_reader_protocol_classes_are_what_the_control_plane_consumes() -> None:
    _, client, _ = build()
    readers = provider_readers(client)
    assert isinstance(readers.prometheus, ConnectorPrometheusReader)
    assert isinstance(readers.loki, ConnectorLokiReader)
    assert isinstance(readers.tempo, ConnectorTempoReader)
    assert isinstance(cluster_reader(client), ConnectorClusterReader)


def test_readers_exist_exactly_for_the_capabilities_the_connector_reports() -> None:
    connector = Connector(providers=ProviderReaders(loki=FakeLoki()))
    client = ConnectorClient(in_process_transport(connector))
    readers = provider_readers(client)
    assert readers.loki is not None and readers.prometheus is None and readers.tempo is None
    assert cluster_reader(client) is None
    assert client.capabilities() == ("logs",)


def test_a_backend_failure_is_raised_never_returned_as_an_empty_result() -> None:
    _, client, cluster = build()
    cluster.fail = True
    with pytest.raises(ConnectorReadError) as raised:
        ConnectorClusterReader(client).list_objects(["shop"])
    assert raised.value.remote_type == "TimeoutError"
    assert "unreachable" in str(raised.value)


def test_a_lost_transport_is_unavailable_not_empty() -> None:
    def down(_: bytes) -> bytes:
        raise ConnectionError("connection refused")

    with pytest.raises(ConnectorUnavailable):
        ConnectorClusterReader(ConnectorClient(down)).list_events(["shop"])
    with pytest.raises(ConnectorUnavailable):
        ConnectorClusterReader(ConnectorClient(lambda _: b"not json")).list_events(["shop"])


def test_an_unconfigured_backend_is_a_failure_not_an_empty_list() -> None:
    connector = Connector()
    response = raw(connector, version=wire.WIRE_VERSION, op="list_objects", args={"namespaces": []})
    assert not response.ok and response.failure is not None
    assert response.failure.error_type == "LookupError"


def test_the_request_is_strict_versioned_and_bounded() -> None:
    connector, _, _ = build()
    assert not raw(connector, version="connector.v0", op="capabilities").ok
    assert not raw(connector, version=wire.WIRE_VERSION, op="apply").ok  # no such operation
    assert not raw(connector, version=wire.WIRE_VERSION, op="capabilities", extra=1).ok
    too_many = {"namespaces": [f"ns{i}" for i in range(wire.MAX_NAMESPACES + 1)]}
    assert not raw(connector, version=wire.WIRE_VERSION, op="list_events", args=too_many).ok
    over = {"target": POD.model_dump(), "query": {"limit": 65}}
    assert not raw(connector, version=wire.WIRE_VERSION, op="query_traffic", args=over).ok
    unknown = {"target": POD.model_dump(), "query": {"limit": 8, "promql": "up"}}
    response = raw(connector, version=wire.WIRE_VERSION, op="query_traffic", args=unknown)
    assert not response.ok and response.failure is not None
    assert response.failure.error_type == "InvalidRequest"  # no query pass-through


def test_an_oversized_response_fails_instead_of_being_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, client, _ = build()
    monkeypatch.setattr(wire, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ConnectorReadError) as raised:
        ConnectorClusterReader(client).list_objects(["shop"])
    assert raised.value.remote_type == "ResponseTooLarge"


def test_every_request_is_audited_by_digest_and_outcome() -> None:
    entries: list[AuditEntry] = []
    _, client, cluster = build(audit=entries)
    ConnectorClusterReader(client).list_events(["shop"])
    cluster.fail = True
    with pytest.raises(ConnectorReadError):
        ConnectorClusterReader(client).list_objects(["shop"])
    assert [(e.op, e.outcome) for e in entries] == [
        ("list_events", "ok"),
        ("list_objects", "failure"),
    ]
    assert all(len(e.request_digest) == 64 and e.response_bytes > 0 for e in entries)


def test_the_bytes_are_canonical_so_equal_requests_have_equal_digests() -> None:
    request = wire.Request(version=wire.WIRE_VERSION, op="capabilities")
    assert wire.dumps(request) == wire.dumps(wire.Request.model_validate_json(wire.dumps(request)))


def test_an_observation_that_would_change_on_the_wire_is_refused() -> None:
    class NanProm(FakeProm):
        def query_traffic(
            self, target: EntityRef, query: InvestigationQuery
        ) -> tuple[TrafficObservation, ...]:
            return (
                TrafficObservation(
                    entity=target, metric="rps", at=T0, value=float("nan"), evidence_id="p:3"
                ),
            )

    connector = Connector(providers=ProviderReaders(prometheus=NanProm()))
    client = ConnectorClient(in_process_transport(connector))
    with pytest.raises(ConnectorReadError) as raised:
        ConnectorPrometheusReader(client).query_traffic(POD, QUERY)
    assert raised.value.remote_type == "EncodingError"
