"""live-trace-design §14.2 P0: concurrent identical trace fetches of a capture are made once, through the Connector.

ControlPlane ProviderAdapter -> ConnectorTempoReader -> ConnectorClient -> encoded bytes -> Connector ->
TempoTraceReader -> Tempo's HTTP API (a fake opener counting the real trace-by-ID requests).
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.request import Request

from provider_test_helpers import provider_session_factory

from packages.connector import wire
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
    """Tempo's HTTP API: each service's search finds the shared trace and its own.

    A fetch of the shared trace is held until a second one arrives, or 0.5 s: two workers reading one slice
    overlap, as ``_capture_traces``'s two workers do, whatever the scheduler does.
    """

    def __init__(self) -> None:
        self.trace_fetches: list[str] = []
        self._lock = threading.Lock()
        self._second = threading.Event()

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if "/api/search?" in url:
            service = next(name for name in OWN if f"%22{name}%22" in url)
            ids = [SHARED, OWN[service]]
            return _Response(json.dumps({"traces": [{"traceID": i} for i in ids]}).encode())
        with self._lock:
            self.trace_fetches.append(url)
            shared = sum(f"/{SHARED}?" in fetch for fetch in self.trace_fetches)
        if f"/{SHARED}?" in url:
            if shared >= 2:
                self._second.set()
            self._second.wait(0.5)
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


def _control_plane_adapter(tempo: _Tempo) -> tuple[ProviderAdapter, Connector]:
    connector = Connector(
        providers=ProviderReaders(
            tempo=TempoTraceReader(TempoConfig("https://tempo.example.internal"), tempo)
        )
    )
    readers = provider_readers(ConnectorClient(in_process_transport(connector)))
    return ProviderAdapter("run", "CAPTURE", provider_session_factory(), readers), connector


def _capture(adapter: ProviderAdapter) -> list[object]:
    """One slice of one capture: two services read at once by the two workers, then the third."""
    start, stop = T0 - timedelta(minutes=1), T0
    with ThreadPoolExecutor(max_workers=2) as pool:
        overlapping = list(
            pool.map(
                lambda service: capture_traces(adapter, [("shop", service)], start, stop),
                SERVICES[:2],
            )
        )
    return [*overlapping, capture_traces(adapter, [("shop", SERVICES[2])], start, stop)]


def test_a_capture_through_the_connector_joins_concurrent_identical_fetches() -> None:
    plain = _Tempo()
    adapter, _ = _control_plane_adapter(plain)
    without = _capture(adapter)
    once = _Tempo()
    adapter, connector = _control_plane_adapter(once)
    with_join = _capture(adapter.fetching_traces_once())

    shared = f"/{SHARED}?"
    assert sum(shared in url for url in plain.trace_fetches) == 3
    # the two overlapping reads share one fetch; the third read, after it, asks Tempo again
    assert sum(shared in url for url in once.trace_fetches) == 2
    assert len(once.trace_fetches) == 5
    assert with_join == without
    preflight = json.loads(
        connector.handle(json.dumps({"version": wire.WIRE_VERSION, "op": "preflight"}).encode())
    )
    assert preflight["result"]["trace_fetches"] == {
        "requests": 6,
        "joined": 1,
        "http_fetches": 5,
        "failures": 0,
        "verify_differences": 0,
    }


def test_an_older_connector_is_never_sent_a_capture_scope() -> None:
    class _OldTempo:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, target: object, query: object) -> tuple[()]:
            self.calls += 1
            return ()

    old = _OldTempo()
    connector = Connector(providers=ProviderReaders(tempo=old))
    assert "trace_capture_scope" not in connector.capabilities()
    sent: list[bytes] = []
    transport = in_process_transport(connector)

    def recording(payload: bytes) -> bytes:
        sent.append(payload)
        return transport(payload)

    readers = provider_readers(ConnectorClient(recording))
    adapter = ProviderAdapter("run", "CAPTURE", provider_session_factory(), readers)
    capture_traces(
        adapter.fetching_traces_once(), [("shop", "order-service")], T0 - timedelta(minutes=1), T0
    )
    assert old.calls == 1
    assert all(b"capture_scope" not in payload for payload in sent)
