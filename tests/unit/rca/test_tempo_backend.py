from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from packages.rca.engine import build_case
from packages.rca.investigation.environment import (
    SourceInvestigationBackend,
    TempoInvestigationBackend,
    initial_view,
)
from packages.rca.investigation.evidence import InMemoryEvidenceStore
from packages.rca.investigation.graph import (
    _check_novelty,
    _execute_tool,
    _Runtime,
    build_investigation_state,
)
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tempo import (
    TempoConfig,
    TempoHttpError,
    TempoProtocolError,
    TempoResponseTooLarge,
    TempoSearchCompleteness,
    TempoTraceReader,
    _compile_target_traceql,
    _TempoHttpResponse,
    parse_tempo_trace_json,
)
from packages.rca.investigation.tools import RuntimeTracesTool
from packages.rca.live import LiveSource
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationQuery,
    TraceSpanStatus,
)
from packages.rca.source import InMemorySource

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TRACE_ID = "a" * 32


def _entity(kind: str = "Deployment", name: str = "payment", namespace: str = "shop") -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace=namespace)


def _attribute(key: str, value: object) -> dict[str, object]:
    field = {
        "string": "stringValue",
        "bool": "boolValue",
        "int": "intValue",
        "double": "doubleValue",
    }
    kind = "string"
    if isinstance(value, bool):
        kind = "bool"
    elif isinstance(value, int):
        kind = "int"
    elif isinstance(value, float):
        kind = "double"
    return {"key": key, "value": {field[kind]: value}}


def _raw_span(
    span_id: str,
    *,
    parent: str | None = None,
    start: int = 0,
    kind: int | str = 2,
    status: int | str = 2,
    attributes: list[dict[str, object]] | None = None,
    trace_id: str = TRACE_ID,
) -> dict[str, object]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent or "0" * 16,
        "name": f"span-{span_id}",
        "kind": kind,
        "startTimeUnixNano": str(int((T0 + timedelta(seconds=start)).timestamp() * 1e9)),
        "endTimeUnixNano": str(int((T0 + timedelta(seconds=start + 1)).timestamp() * 1e9)),
        "status": {"code": status},
        "attributes": attributes or [],
    }


def _trace_payload(
    spans: list[dict[str, object]], *, service: str = "payment"
) -> dict[str, object]:
    resource_attributes = [
        _attribute("service.name", service),
        _attribute("k8s.namespace.name", "shop"),
        _attribute("k8s.deployment.name", "payment"),
    ]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attributes},
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> _TempoHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


class _Transport:
    def __init__(self, responses: dict[str, _Response | bytes | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, str], str]] = []

    def __call__(self, request: Request, *, timeout: float) -> _TempoHttpResponse:
        del timeout
        url = request.full_url
        method = request.get_method()
        headers = dict(request.header_items())
        self.requests.append((url, headers, method))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return _Response(response)
        return response


def _reader(transport: _Transport) -> TempoTraceReader:
    return TempoTraceReader(
        TempoConfig("https://tempo.example.internal", tenant_id="tenant"), transport
    )


def _query() -> InvestigationQuery:
    return InvestigationQuery(
        start=T0 - timedelta(minutes=1), end=T0 + timedelta(minutes=1), limit=32
    )


def test_config_validation_and_secret_free_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPO_URL", "https://tempo.example.internal/")
    monkeypatch.setenv("TEMPO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("TEMPO_BEARER_TOKEN", "secret-token")
    monkeypatch.setenv("TEMPO_TIMEOUT_SECONDS", "4")
    config = TempoConfig.from_environment()
    assert config is not None
    assert config.base_url == "https://tempo.example.internal"
    assert config.timeout_seconds == 4.0
    assert config.tenant_id == "tenant-a"
    assert "secret-token" not in repr(config)
    monkeypatch.delenv("TEMPO_URL")
    assert TempoConfig.from_environment() is None
    for value in (
        "ftp://tempo",
        "https://user:pass@tempo",
        "https://tempo/path?x=1",
        "https://tempo/#fragment",
    ):
        with pytest.raises(ValueError):
            TempoConfig(value)


@pytest.mark.parametrize(
    ("kind", "attribute"),
    [
        ("Pod", "k8s.pod.name"),
        ("Deployment", "k8s.deployment.name"),
        ("StatefulSet", "k8s.statefulset.name"),
        ("DaemonSet", "k8s.daemonset.name"),
    ],
)
def test_traceql_is_server_owned_and_exact(kind: str, attribute: str) -> None:
    query = _compile_target_traceql(_entity(kind, 'pay\\"ment'))
    assert 'resource."k8s.namespace.name"' in query
    assert f'resource."{attribute}"' in query
    assert "service.name" not in query
    assert "status" not in query
    assert "\\\\" in query
    assert '\\"' in query
    with pytest.raises(ValueError):
        _compile_target_traceql(_entity("Service"))
    with pytest.raises(ValueError):
        _compile_target_traceql(_entity("Pod", "bad\nname"))


def test_bounded_search_request_and_headers() -> None:
    search_url = "https://tempo.example.internal/api/search"
    transport = _Transport({f"{search_url}?": b"{}"})

    # Match by inspecting the generated URL while retaining a deterministic fake opener.
    def opener(request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        transport.requests.append((url, dict(request.header_items()), request.get_method()))
        return _Response(json.dumps({"traces": []}).encode())

    reader = TempoTraceReader(
        TempoConfig(
            "https://tempo.example.internal",
            tenant_id="tenant-a",
            bearer_token="secret",
        ),
        opener,
    )
    reader.query(_entity("Pod", "payment-0"), _query())
    url, headers, method = transport.requests[0]
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert parsed.path == "/api/search"
    assert method == "GET"
    assert params["limit"] == ["8"]
    assert params["spss"] == ["1"]
    assert params["start"] == [str(int((_query().start or T0).timestamp()))]
    assert params["end"] == [str(int((_query().end or T0).timestamp()))]
    assert 'resource."k8s.pod.name"' in params["q"][0]
    assert headers["Accept"] == "application/json"
    assert headers["X-scope-orgid"] == "tenant-a"
    assert headers["Authorization"] == "Bearer secret"


def test_bounded_query_rejects_without_http() -> None:
    transport = _Transport({})
    reader = _reader(transport)
    for query in (
        InvestigationQuery(),
        InvestigationQuery(start=T0, end=T0 - timedelta(seconds=1)),
        InvestigationQuery(start=T0, end=T0 + timedelta(seconds=3601)),
    ):
        with pytest.raises(ValueError):
            reader.query(_entity(), query)
    assert transport.requests == []


def test_search_ids_are_sorted_deduplicated_and_truncated() -> None:
    ids = [f"{index:032x}" for index in range(9)] + ["A" * 32]
    body = json.dumps({"traces": [{"traceID": value} for value in reversed(ids)]}).encode()
    transport = _Transport({})

    def opener(request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        transport.requests.append((url, dict(request.header_items()), request.get_method()))
        if url.startswith("https://tempo.example.internal/api/search?"):
            return _Response(body)
        return _Response(json.dumps({"resourceSpans": []}).encode())

    batch = TempoTraceReader(TempoConfig("https://tempo.example.internal"), opener).query(
        _entity(), _query()
    )
    diagnostics = batch.diagnostics
    assert diagnostics.completeness is TempoSearchCompleteness.TRUNCATED
    assert diagnostics.search_limit_reached
    assert diagnostics.candidate_trace_ids == tuple(sorted({value.lower() for value in ids}))[:8]
    assert len(diagnostics.fetched_trace_ids) == 8


def test_fetch_404_is_missing_but_other_errors_fail() -> None:
    first = f"{0:032x}"
    second = f"{1:032x}"
    search_url = "https://tempo.example.internal/api/search"
    calls: list[str] = []

    def opener(request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        calls.append(url)
        if url.startswith(search_url):
            return _Response(
                json.dumps({"traces": [{"traceID": first}, {"traceID": second}]}).encode()
            )
        if f"/{first}?" in url:
            return _Response(json.dumps(_trace_payload([_raw_span("1" * 16)])).encode())
        raise HTTPError(url, 404, "missing", Message(), None)

    batch = TempoTraceReader(TempoConfig("https://tempo.example.internal"), opener).query(
        _entity(), _query()
    )
    assert batch.spans
    assert batch.diagnostics.missing_trace_ids == (second,)
    assert calls[0].startswith(search_url)

    def server_error(request: Request, *, timeout: float) -> _Response:
        del request, timeout
        raise HTTPError(search_url, 500, "failure", Message(), None)

    with pytest.raises(TempoHttpError):
        TempoTraceReader(TempoConfig("https://tempo.example.internal"), server_error).query(
            _entity(), _query()
        )


def test_otlp_parser_scalar_attributes_kinds_status_and_ids() -> None:
    payload = _trace_payload(
        [
            _raw_span(
                "1" * 16,
                kind="SPAN_KIND_SERVER",
                status="STATUS_CODE_ERROR",
                attributes=[
                    _attribute("k8s.pod.name", "payment-override"),
                    _attribute("custom.not-allowed", "ignored"),
                    _attribute("k8s.container.name", True),
                ],
            )
        ]
    )
    span = parse_tempo_trace_json(payload)[0]
    assert span.evidence_id == f"tempo:{TRACE_ID}:1111111111111111"
    assert span.service == "payment"
    assert span.semantic_attributes["k8s.pod.name"] == "payment-override"
    assert "custom.not-allowed" not in span.semantic_attributes
    assert span.semantic_attributes["k8s.container.name"] == "True"
    assert span.span_kind == "SERVER"
    assert span.status is TraceSpanStatus.ERROR
    assert span.start_at == T0
    assert span.end_at == T0 + timedelta(seconds=1)
    legacy = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attribute("service.name", "payment"),
                        _attribute("k8s.namespace.name", "shop"),
                    ]
                },
                "instrumentationLibrarySpans": [{"spans": [_raw_span("2" * 16, kind=5, status=1)]}],
            }
        ]
    }
    parsed = parse_tempo_trace_json(legacy)[0]
    assert parsed.span_kind == "CONSUMER"
    assert parsed.status is TraceSpanStatus.OK


def test_parser_duplicate_and_malformed_records_fail_closed() -> None:
    raw = _raw_span("1" * 16)
    payload = _trace_payload([raw, dict(raw)])
    assert len(parse_tempo_trace_json(payload)) == 1
    conflicting = dict(raw, name="different")
    with pytest.raises(TempoProtocolError):
        parse_tempo_trace_json(_trace_payload([raw, conflicting]))
    with pytest.raises(TempoProtocolError):
        parse_tempo_trace_json(_trace_payload([dict(raw, traceId="../../etc/passwd")]))
    with pytest.raises(TempoProtocolError):
        parse_tempo_trace_json(
            {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [raw]}]}]}
        )


def test_parser_accepts_tempo_v2_trace_envelope() -> None:
    raw = _raw_span("3" * 16)
    raw["traceId"] = base64.b64encode(bytes.fromhex(TRACE_ID)).decode("ascii")
    raw["spanId"] = base64.b64encode(bytes.fromhex("3" * 16)).decode("ascii")
    payload = {"trace": _trace_payload([raw])}

    spans = parse_tempo_trace_json(payload)

    assert len(spans) == 1
    assert spans[0].evidence_id == f"tempo:{TRACE_ID}:3333333333333333"
    assert spans[0].semantic_attributes["k8s.deployment.name"] == "payment"


def test_response_limits_and_search_payload_is_not_evidence() -> None:
    query = _query()

    def large_search(request: Request, *, timeout: float) -> _Response:
        del request, timeout
        return _Response(b"x" * (1_048_576 + 1))

    with pytest.raises(TempoResponseTooLarge):
        TempoTraceReader(TempoConfig("https://tempo.example.internal"), large_search).query(
            _entity(), query
        )

    def large_trace(request: Request, *, timeout: float) -> _Response:
        del timeout
        if "/api/search?" in request.full_url:
            return _Response(json.dumps({"traces": [{"traceID": TRACE_ID}]}).encode())
        return _Response(b"x" * (4_194_304 + 1))

    with pytest.raises(TempoResponseTooLarge):
        TempoTraceReader(TempoConfig("https://tempo.example.internal"), large_trace).query(
            _entity(), query
        )

    def search_only(request: Request, *, timeout: float) -> _Response:
        del timeout
        if "/api/search?" in request.full_url:
            return _Response(json.dumps({"traces": [], "spanSets": [_raw_span("1" * 16)]}).encode())
        raise AssertionError("search payload should not trigger a trace fetch")

    batch = TempoTraceReader(TempoConfig("https://tempo.example.internal"), search_only).query(
        _entity(), query
    )
    assert batch.spans == ()


def test_tempo_backend_reuses_a2_one_hop_selection_and_live_support_is_optional() -> None:
    parent = _raw_span(
        "1" * 16,
        parent=None,
        attributes=[_attribute("k8s.deployment.name", "frontend")],
    )
    seed = _raw_span(
        "2" * 16,
        parent="1" * 16,
        attributes=[_attribute("k8s.deployment.name", "payment")],
    )
    child = _raw_span(
        "3" * 16, parent="2" * 16, attributes=[_attribute("k8s.deployment.name", "orders")]
    )
    grandchild = _raw_span("4" * 16, parent="3" * 16)
    unrelated = _raw_span("5" * 16, attributes=[_attribute("k8s.deployment.name", "other")])

    def opener(request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if "/api/search?" in url:
            return _Response(json.dumps({"traces": [{"traceID": TRACE_ID}]}).encode())
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            _attribute("service.name", "payment"),
                            _attribute("k8s.namespace.name", "shop"),
                        ]
                    },
                    "scopeSpans": [{"spans": [parent, seed, child, grandchild, unrelated]}],
                }
            ]
        }
        return _Response(json.dumps(payload).encode())

    reader = TempoTraceReader(TempoConfig("https://tempo.example.internal"), opener)
    tempo_backend = TempoInvestigationBackend(
        base=SourceInvestigationBackend(InMemorySource(name="base")),
        tempo=reader,
        observation_cutoff=T0 + timedelta(minutes=1),
    )
    selected = tempo_backend.query_traces(_entity(), _query())
    assert tuple(span.span_id for span in selected) == (
        "1111111111111111",
        "2222222222222222",
        "3333333333333333",
    )

    source = LiveSource(
        incident="live",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
    )
    assert not source.supports("runtime_traces")
    assert source.trace_observations() == []
    configured = LiveSource(
        incident="live-configured",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
        tempo_reader=reader,
    )
    assert configured.supports("runtime_traces")
    assert isinstance(configured.investigation_backend(), TempoInvestigationBackend)
    assert configured.trace_observations() == []


def test_provider_acquisition_uses_real_graph_materialization_path() -> None:
    target = _entity()
    parent = _raw_span("1" * 16, kind=3, status=2)
    seed = _raw_span(
        "2" * 16,
        parent="1" * 16,
        kind=2,
        status=2,
        attributes=[_attribute("k8s.deployment.name", "payment")],
    )

    def opener(request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if "/api/search?" in url:
            return _Response(json.dumps({"traces": [{"traceID": TRACE_ID}]}).encode())
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            _attribute("service.name", "frontend"),
                            _attribute("k8s.namespace.name", "shop"),
                        ]
                    },
                    "scopeSpans": [{"spans": [parent]}],
                },
                {
                    "resource": {
                        "attributes": [
                            _attribute("service.name", "payment"),
                            _attribute("k8s.namespace.name", "shop"),
                        ]
                    },
                    "scopeSpans": [{"spans": [seed]}],
                },
            ]
        }
        return _Response(json.dumps(payload).encode())

    source = InMemorySource(name="provider-graph")
    bounded = initial_view(source)
    base_case = build_case(bounded)
    gap = InformationGap(
        gap_id="gap:provider-traces",
        dimension=GapDimension.DEPENDENCY_HEALTH,
        missing_fact="runtime trace evidence",
        authorized_queries=(AuthorizedQuery(capability="runtime_traces", target=target),),
        candidate_tools=("runtime_traces",),
        entity_scope=(target,),
        resolvability=GapResolvability.RESOLVABLE,
    )
    action = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability="runtime_traces",
        target=target,
        query=_query(),
    )
    tempo = TempoTraceReader(TempoConfig("https://tempo.example.internal"), opener)
    backend = TempoInvestigationBackend(
        base=SourceInvestigationBackend(source),
        tempo=tempo,
        observation_cutoff=T0 + timedelta(minutes=1),
    )
    state = build_investigation_state(bounded, initial_case=base_case)
    state["current_diagnosis"] = state["current_diagnosis"].model_copy(
        update={"information_gaps": (gap,)}
    )
    state["pending_action"] = action
    runtime = _Runtime(
        source=bounded,
        policy=ScriptedInvestigationPolicy([]),
        tools={"runtime_traces": RuntimeTracesTool(backend)},
        config=InvestigationConfig(),
        initial_case=base_case,
        rebuild_case=None,
        backend=backend,
        evidence_store=InMemoryEvidenceStore(),
    )
    observed = _execute_tool(state, runtime)
    state["pending_observation"] = observed["pending_observation"]
    acquired = _check_novelty(state, runtime)
    assert acquired["pending_new_evidence_refs"]
    rebuilt = runtime.case_for(acquired["acquired_evidence_refs"], ())
    assert rebuilt.source.trace_observations()
    assert rebuilt.runtime_graph.edges
    assert rebuilt.runtime_evidence.service_outcomes
    assert rebuilt.runtime_propagation.edges
    assert state["investigation_findings"] == ()
