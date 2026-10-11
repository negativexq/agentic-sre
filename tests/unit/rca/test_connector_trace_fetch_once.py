"""live-trace-design §14 P0: a capture fetches an identical trace once on the path the lab uses, through the Connector.

ControlPlane ProviderAdapter -> ConnectorTempoReader -> ConnectorClient -> encoded bytes -> Connector ->
TempoTraceReader -> Tempo's HTTP API (a fake opener counting the real trace-by-ID requests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.request import Request

import pytest
from provider_test_helpers import provider_session_factory

from packages.connector.client import ConnectorClient, in_process_transport, provider_readers
from packages.connector.service import Connector
from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
from packages.rca.live import capture_traces
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SERVICES = ("order-service", "payment-service", "order-worker")
SHARED = "b" * 32
OWN = {"order-service": "c" * 32, "payment-service": "d" * 32, "order-worker": "e" * 32}


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _span(trace_id: str, span_id: str, service: str) -> dict[str, object]:
    start = int(T0.timestamp() * 1e9)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": "0" * 16,
        "name": f"{service} span",
        "kind": 2,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 1_000_000),
        "status": {"code": 1},
        "attributes": [],
    }


class _Tempo:
    """Tempo's HTTP API: each service's search finds the shared trace and its own."""

    def __init__(self) -> None:
        self.trace_fetches: list[str] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if "/api/search?" in url:
            service = next(name for name in OWN if f"%22{name}%22" in url)
            ids = [SHARED, OWN[service]]
            return _Response(json.dumps({"traces": [{"traceID": i} for i in ids]}).encode())
        self.trace_fetches.append(url)
        trace_id = next(i for i in (SHARED, *OWN.values()) if f"/{i}?" in url)
        resource = [
            {"key": "service.name", "value": {"stringValue": "order-service"}},
            {"key": "k8s.namespace.name", "value": {"stringValue": "shop"}},
            {"key": "k8s.deployment.name", "value": {"stringValue": "order-service"}},
        ]
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource},
                    "scopeSpans": [{"spans": [_span(trace_id, trace_id[:16], "order-service")]}],
                }
            ]
        }
        return _Response(json.dumps(payload).encode())


def _control_plane_adapter(tempo: _Tempo) -> ProviderAdapter:
    connector = Connector(
        providers=ProviderReaders(
            tempo=TempoTraceReader(TempoConfig("https://tempo.example.internal"), tempo)
        )
    )
    readers = provider_readers(ConnectorClient(in_process_transport(connector)))
    return ProviderAdapter("run", "CAPTURE", provider_session_factory(), readers)


def _capture(adapter: ProviderAdapter) -> list[object]:
    """One capture: the services' reads of one slice, as ``_capture_traces`` makes them."""
    start, stop = T0 - timedelta(minutes=1), T0
    return [capture_traces(adapter, [("shop", service)], start, stop) for service in SERVICES]


@pytest.mark.xfail(
    strict=True, reason="live-trace §14 P0: the Connector path does not fetch once yet"
)
def test_a_capture_through_the_connector_fetches_an_identical_trace_once() -> None:
    plain = _Tempo()
    without = _capture(_control_plane_adapter(plain))
    once = _Tempo()
    with_reuse = _capture(_control_plane_adapter(once).fetching_traces_once())

    assert len(plain.trace_fetches) == 6
    assert with_reuse == without
    # the shared trace reaches Tempo once, each service's own trace once
    assert len(once.trace_fetches) == len(set(once.trace_fetches)) == 4
