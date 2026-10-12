"""live-trace-design §15: a slice's services keep their own searches, and a trace several found is fetched once.

ControlPlane ProviderAdapter -> ConnectorTempoReader -> ConnectorClient -> encoded bytes -> Connector ->
TempoTraceReader -> Tempo's HTTP API (a fake opener counting the real requests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from provider_test_helpers import provider_session_factory

from apps.control_plane.trace_admission import ADMISSION_TIMEOUT, FifoSlots, read_slices
from packages.connector.client import ConnectorClient, in_process_transport, provider_readers
from packages.connector.service import Connector
from packages.rca.investigation.tempo import TempoConfig, TempoTraceBatch, TempoTraceReader
from packages.rca.live import TraceCapture, capture_traces
from packages.rca.model import EntityRef, InvestigationQuery
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.storage.repositories import InvestigationReadRepository

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
START, STOP = T0 - timedelta(minutes=1), T0
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
        return 200

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _span(trace_id: str, span_id: str, service: str) -> dict[str, object]:
    start = int(T0.timestamp() * 1e9) - 30_000_000_000
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
    """Each service's search finds the shared trace and its own; a trace can gain a span, be missing or fail."""

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.fetches: list[str] = []
        self.late: set[str] = set()  # traces that gain a consumer span from their second fetch on
        self.missing: set[str] = set()
        self.failing: set[str] = set()
        self.failing_search: set[str] = set()

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if "/api/search?" in url:
            service = next(name for name in OWN if f"%22{name}%22" in url)
            self.searches.append(service)
            if service in self.failing_search:
                raise HTTPError(url, 500, "failure", Message(), None)
            ids = [SHARED, OWN[service]]
            return _Response(json.dumps({"traces": [{"traceID": i} for i in ids]}).encode())
        trace_id = next(i for i in (SHARED, *OWN.values()) if f"/{i}?" in url)
        self.fetches.append(trace_id)
        if trace_id in self.failing:
            raise HTTPError(url, 500, "failure", Message(), None)
        if trace_id in self.missing:
            raise HTTPError(url, 404, "missing", Message(), None)
        spans = [_span(trace_id, trace_id[:16], "order-service")]
        if trace_id in self.late and self.fetches.count(trace_id) > 1:
            spans.append(_span(trace_id, "f" * 16, "order-worker"))
        resource = [{"key": "service.name", "value": {"stringValue": "order-service"}}]
        payload = {
            "resourceSpans": [
                {"resource": {"attributes": resource}, "scopeSpans": [{"spans": spans}]}
            ]
        }
        return _Response(json.dumps(payload).encode())


def _adapter(tempo: Any, *, connector: bool = True) -> ProviderAdapter:
    reader = TempoTraceReader(TempoConfig("https://tempo.example.internal"), tempo)
    if connector:
        client = ConnectorClient(
            in_process_transport(Connector(providers=ProviderReaders(tempo=reader)))
        )
        readers = provider_readers(client)
    else:
        readers = ProviderReaders(tempo=reader)
    return ProviderAdapter("run", "CAPTURE", provider_session_factory(), readers)


def _slice(adapter: ProviderAdapter) -> TraceCapture:
    return capture_traces(adapter, [("shop", s) for s in SERVICES], START, STOP)


def _one_by_one(adapter: ProviderAdapter) -> TraceCapture:
    """The slice as it was read before §15: each service on its own."""
    parts = [capture_traces(adapter, [("shop", s)], START, STOP) for s in SERVICES]
    spans = {(span.trace_id, span.span_id): span for part in parts for span in part.spans}
    return TraceCapture(
        tuple(
            sorted(spans.values(), key=lambda span: (span.start_at, span.trace_id, span.span_id))
        ),
        tuple(read for part in parts for read in part.reads),
    )


def _reads(adapter: ProviderAdapter) -> list[tuple[str, dict[str, Any] | None, str | None]]:
    with adapter.session_factory() as session:
        rows = InvestigationReadRepository(session).list_for_run("run")
        return [(r.query_descriptor["target"], r.observation, r.error_type) for r in rows]


def _service_view(capture: TraceCapture) -> list[tuple[str, str, int]]:
    return [(r.service, r.completeness, r.spans) for r in capture.reads]


@pytest.mark.parametrize("connector", [True, False])
def test_a_slice_fetches_each_trace_once_and_every_service_keeps_its_own_read(
    connector: bool,
) -> None:
    before, after = _Tempo(), _Tempo()
    one_by_one = _adapter(before, connector=connector)
    old = _one_by_one(one_by_one)
    slice_once = _adapter(after, connector=connector).fetching_traces_once()
    new = _slice(slice_once)

    assert sorted(before.searches) == sorted(after.searches) == sorted(SERVICES)
    assert before.fetches.count(SHARED) == 3 and len(before.fetches) == 6
    assert after.fetches.count(SHARED) == 1 and len(after.fetches) == 4
    # evidence equivalence: the same spans, the same per-service reads, the same recorded observations
    assert new == old
    assert _reads(slice_once) == _reads(one_by_one)
    for target, observation, _ in _reads(slice_once):
        assert observation is not None
        own = OWN[target.rsplit("/", 1)[1]]
        assert observation["diagnostics"]["candidate_trace_ids"] == [SHARED, own]
        assert observation["diagnostics"]["fetched_trace_ids"] == [SHARED, own]


def test_the_next_slice_read_fetches_again_and_sees_a_late_consumer_span() -> None:
    tempo = _Tempo()
    tempo.late.add(SHARED)
    adapter = _adapter(tempo).fetching_traces_once()
    first, second = _slice(adapter), _slice(adapter)
    assert tempo.fetches.count(SHARED) == 2
    assert "f" * 16 not in {span.span_id for span in first.spans}
    assert "f" * 16 in {span.span_id for span in second.spans}


def test_a_failed_fetch_fails_only_the_reads_that_selected_it() -> None:
    tempo = _Tempo()
    tempo.failing.add(OWN["payment-service"])
    tempo.missing.add(OWN["order-worker"])
    adapter = _adapter(tempo).fetching_traces_once()
    capture = _slice(adapter)
    assert _service_view(capture)[1][:2] == ("payment-service", "FAILED")
    assert [r.completeness for r in capture.reads] == ["BEST_EFFORT", "FAILED", "BEST_EFFORT"]
    worker = _reads(adapter)[2][1]
    assert worker is not None and worker["diagnostics"]["missing_trace_ids"] == [
        OWN["order-worker"]
    ]
    assert _reads(adapter)[1][2] == "ConnectorReadError"


def test_a_failed_search_fails_only_its_own_read_and_nothing_it_alone_needed_is_fetched() -> None:
    tempo = _Tempo()
    tempo.failing_search.add("payment-service")
    capture = _slice(_adapter(tempo).fetching_traces_once())
    assert [r.completeness for r in capture.reads] == ["BEST_EFFORT", "FAILED", "BEST_EFFORT"]
    assert OWN["payment-service"] not in tempo.fetches and tempo.fetches.count(SHARED) == 1


def test_a_single_target_reader_stops_at_its_first_failed_fetch_as_before() -> None:
    tempo = _Tempo()
    tempo.failing.add(SHARED)
    reader = TempoTraceReader(TempoConfig("https://tempo.example.internal"), tempo)
    query = InvestigationQuery(start=START, end=STOP, limit=32)
    target = EntityRef(kind="Deployment", name="order-service", namespace="shop")
    with pytest.raises(Exception, match="500"):
        reader.query(target, query)
    assert tempo.fetches == [SHARED]
    (result,) = reader.query_many((target,), query)
    assert isinstance(result, Exception) and not isinstance(result, TempoTraceBatch)


def test_an_older_connector_is_read_one_service_at_a_time() -> None:
    class _OldTempo:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def fetching_once(self, capture_scope: str | None = None) -> _OldTempo:
            return self

        def query(self, target: EntityRef, query: object) -> tuple[()]:
            self.targets.append(target.name)
            return ()

    old = _OldTempo()
    connector = Connector(providers=ProviderReaders(tempo=old))
    assert "trace_slice_fetch" not in connector.capabilities()
    sent: list[bytes] = []
    transport = in_process_transport(connector)

    def recording(payload: bytes) -> bytes:
        sent.append(payload)
        return transport(payload)

    readers = provider_readers(ConnectorClient(recording))
    adapter = ProviderAdapter("run", "CAPTURE", provider_session_factory(), readers)
    capture = _slice(adapter.fetching_traces_once())
    assert old.targets == list(SERVICES)
    assert [r.completeness for r in capture.reads] == ["BEST_EFFORT"] * 3
    assert all(b"query_traces_many" not in payload for payload in sent)


def test_a_slice_is_admitted_once_and_a_timed_out_slice_fails_each_of_its_reads() -> None:
    seen: list[tuple[str, ...]] = []

    def capture(
        reader: object, services: list[tuple[str, str]], start: datetime, stop: datetime
    ) -> TraceCapture:
        seen.append(tuple(service for _, service in services))
        return TraceCapture((), ())

    later = (T0, T0 + timedelta(minutes=1))
    reads = [("shop", s, START, STOP) for s in SERVICES] + [
        ("shop", s, *later) for s in SERVICES[:2]
    ]
    read_slices(object(), reads, FifoSlots(2), capture=capture)
    assert seen == [SERVICES, SERVICES[:2]]

    blocked = FifoSlots(0)
    (timed_out,) = read_slices(object(), reads[:3], blocked, wait=0.01, capture=capture)
    assert [(r.service, r.error) for r in timed_out.reads] == [
        (s, ADMISSION_TIMEOUT) for s in SERVICES
    ]
