from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import TypedDict, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from packages.rca.engine import Case, build_case
from packages.rca.investigation.environment import (
    PrometheusInvestigationBackend,
    SourceInvestigationBackend,
    initial_view,
)
from packages.rca.investigation.evidence import InMemoryEvidenceStore
from packages.rca.investigation.graph import (
    _check_novelty,
    _execute_tool,
    _Runtime,
    build_investigation_state,
)
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.prometheus import (
    PrometheusConfig,
    PrometheusHttpError,
    PrometheusMetricsReader,
    PrometheusProtocolError,
    PrometheusResponseTooLarge,
)
from packages.rca.investigation.state import InvestigationConfig, InvestigationTool
from packages.rca.investigation.tools import ResourcePressureTool, TrafficTool
from packages.rca.live import LiveSource
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    EntityRef,
    FindingKind,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationObservation,
    InvestigationQuery,
    ObjectVersion,
    RuntimeObservationState,
)
from packages.rca.source import InMemorySource

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str = "Pod", name: str = "payment-0", namespace: str = "shop") -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace=namespace)


def _query(
    *,
    start: datetime = T0 - timedelta(minutes=5),
    end: datetime = T0 + timedelta(minutes=5),
    limit: int = 32,
) -> InvestigationQuery:
    return InvestigationQuery(start=start, end=end, limit=limit)


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


class _Transport:
    def __init__(self, handler: Callable[[str, dict[str, str]], _Response]) -> None:
        self.handler = handler
        self.requests: list[tuple[str, dict[str, str], str]] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        headers = dict(request.header_items())
        self.requests.append((url, headers, request.get_method()))
        return self.handler(url, headers)


def _success(series: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": series}}
    ).encode()


def _series(values: list[list[object]], *, container: str | None = "app") -> dict[str, object]:
    return {"metric": ({"container": container} if container is not None else {}), "values": values}


def _sample(at: datetime, value: object) -> list[object]:
    return [at.timestamp(), value]


def _reader(transport: _Transport) -> PrometheusMetricsReader:
    return PrometheusMetricsReader(
        PrometheusConfig("https://prometheus.example.internal", tenant_id="tenant"),
        opener=transport,
    )


def test_config_environment_validation_and_secret_free_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMETHEUS_URL", "https://prometheus.example.internal/")
    monkeypatch.setenv("PROMETHEUS_TENANT_ID", "tenant-a")
    monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "secret-token")
    monkeypatch.setenv("PROMETHEUS_TIMEOUT_SECONDS", "4")
    config = PrometheusConfig.from_environment()
    assert config is not None
    assert config.base_url == "https://prometheus.example.internal"
    assert config.timeout_seconds == 4.0
    assert "secret-token" not in repr(config)
    monkeypatch.delenv("PROMETHEUS_URL")
    assert PrometheusConfig.from_environment() is None
    for value in (
        "ftp://prometheus",
        "https://user:pass@prometheus",
        "https://prometheus/path?query=x",
        "https://prometheus/#fragment",
    ):
        with pytest.raises(ValueError):
            PrometheusConfig(value)
    for timeout_value in (0, 0.01, 31, -1, "bad"):
        with pytest.raises(ValueError):
            PrometheusConfig(
                "https://prometheus",
                timeout_seconds=timeout_value,  # type: ignore[arg-type]
            )


def test_query_bounds_label_escaping_and_exact_server_queries() -> None:
    calls: list[str] = []

    def handler(url: str, headers: dict[str, str]) -> _Response:
        del headers
        calls.append(url)
        return _Response(_success([]))

    transport = _Transport(handler)
    reader = _reader(transport)
    reader.query_traffic(_entity("Service", 'payment\\"service'), _query())
    parsed = urlsplit(calls[0])
    params = parse_qs(parsed.query)
    assert parsed.path == "/api/v1/query_range"
    assert set(params) == {"query", "start", "end", "step"}
    assert "http_requests_total" in params["query"][0]
    assert 'service="payment\\\\\\"service"' in params["query"][0]
    assert 'route!~"/(health|metrics|__faults).*"' in params["query"][0]
    assert "status" not in params["query"][0]
    assert params["step"] == ["20"]
    with pytest.raises(ValueError):
        reader.query_traffic(_entity("Service", "bad\nname"), _query())
    for invalid in (
        InvestigationQuery(),
        InvestigationQuery(start=T0, end=T0 - timedelta(seconds=1)),
        InvestigationQuery(start=T0, end=T0 + timedelta(seconds=3601)),
        InvestigationQuery(start=datetime(2026, 1, 1, 12), end=T0),
    ):
        with pytest.raises(ValueError):
            reader.query_traffic(_entity("Service"), invalid)


def test_resource_queries_are_fixed_and_pressure_target_is_pod_only() -> None:
    queries: list[str] = []

    def handler(url: str, headers: dict[str, str]) -> _Response:
        del headers
        query = parse_qs(urlsplit(url).query)["query"][0]
        queries.append(query)
        return _Response(_success([]))

    transport = _Transport(handler)
    reader = _reader(transport)
    reader.query_resource_pressure(_entity(), _query())
    assert "container_memory_working_set_bytes" in queries[0]
    assert "kube_pod_container_resource_limits" in queries[0]
    assert 'namespace="shop"' in queries[0]
    assert 'pod="payment-0"' in queries[0]
    assert 'resource="memory"' in queries[0]
    assert 'unit="byte"' in queries[0]
    assert "container_cpu_cfs_throttled_periods_total" in queries[1]
    assert "container_cpu_cfs_periods_total" in queries[1]
    assert "[1m]" in queries[1]
    with pytest.raises(ValueError):
        reader.query_resource_pressure(_entity("Deployment", "payment"), _query())
    assert len(transport.requests) == 2


def test_matrix_parser_ordering_duplicates_and_nonfinite_values() -> None:
    def handler(url: str, headers: dict[str, str]) -> _Response:
        del url, headers
        return _Response(
            _success(
                [
                    _series(
                        [
                            _sample(T0 + timedelta(seconds=30), "NaN"),
                            _sample(T0 + timedelta(seconds=20), "0.5"),
                            _sample(T0, "0.2"),
                            _sample(T0, "0.2"),
                            _sample(T0 + timedelta(seconds=10), "-1"),
                            _sample(T0 + timedelta(seconds=40), "0.9"),
                        ]
                    )
                ]
            )
        )

    reader = _reader(_Transport(handler))
    records = reader.query_resource_pressure(_entity(), _query())
    assert len(records) == 2
    memory = next(item for item in records if item.resource == "memory")
    assert memory.baseline == 0.2
    assert memory.peak == 0.9
    assert memory.at == T0 + timedelta(seconds=40)
    assert memory.evidence_id.startswith("prometheus:resource:")


def test_conflicting_duplicate_sample_and_protocol_shapes_fail() -> None:
    def conflicting(url: str, headers: dict[str, str]) -> _Response:
        del url, headers
        return _Response(_success([_series([_sample(T0, "1"), _sample(T0, "2")])]))

    reader = _reader(_Transport(conflicting))
    with pytest.raises(PrometheusProtocolError):
        reader.query_traffic(_entity("Service"), _query())

    def bad_result(url: str, headers: dict[str, str]) -> _Response:
        del url, headers
        return _Response(
            json.dumps(
                {"status": "success", "data": {"resultType": "vector", "result": []}}
            ).encode()
        )

    with pytest.raises(PrometheusProtocolError):
        PrometheusMetricsReader(
            PrometheusConfig("https://prometheus"), opener=_Transport(bad_result)
        ).query_traffic(_entity("Service"), _query())


def test_http_contract_auth_size_and_errors() -> None:
    def empty(url: str, headers: dict[str, str]) -> _Response:
        del url
        normalized = {key.lower(): value for key, value in headers.items()}
        assert normalized["accept"] == "application/json"
        assert normalized["x-scope-orgid"] == "tenant"
        assert normalized["authorization"] == "Bearer token"
        return _Response(_success([]))

    transport = _Transport(empty)
    PrometheusMetricsReader(
        PrometheusConfig(
            "https://prometheus.example.internal",
            tenant_id="tenant",
            bearer_token="token",
        ),
        opener=transport,
    ).query_traffic(_entity("Service"), _query())
    assert transport.requests[0][2] == "GET"

    def too_large(url: str, headers: dict[str, str]) -> _Response:
        del url, headers
        return _Response(b"x" * (2_097_152 + 1))

    with pytest.raises(PrometheusResponseTooLarge):
        PrometheusMetricsReader(
            PrometheusConfig("https://prometheus"), opener=_Transport(too_large)
        ).query_traffic(_entity("Service"), _query())

    def server_error(url: str, headers: dict[str, str]) -> _Response:
        del headers
        raise HTTPError(url, 500, "error", Message(), None)

    with pytest.raises(PrometheusHttpError):
        PrometheusMetricsReader(
            PrometheusConfig("https://prometheus"), opener=_Transport(server_error)
        ).query_traffic(_entity("Service"), _query())

    def api_error(url: str, headers: dict[str, str]) -> _Response:
        del url, headers
        return _Response(json.dumps({"status": "error", "errorType": "execution"}).encode())

    with pytest.raises(PrometheusProtocolError):
        PrometheusMetricsReader(
            PrometheusConfig("https://prometheus"), opener=_Transport(api_error)
        ).query_traffic(_entity("Service"), _query())


def test_traffic_step_spans_window_and_evidence_is_stable() -> None:
    query = _query(start=T0 - timedelta(minutes=30), end=T0 + timedelta(minutes=30), limit=32)
    observed: list[dict[str, list[str]]] = []

    def handler(url: str, headers: dict[str, str]) -> _Response:
        del headers
        params = parse_qs(urlsplit(url).query)
        observed.append(params)
        return _Response(
            _success(
                [
                    {
                        "metric": {},
                        "values": [
                            _sample(T0 - timedelta(seconds=1), "10"),
                            _sample(T0, "10"),
                            _sample(T0 + timedelta(seconds=1), "20"),
                        ],
                    }
                ]
            )
        )

    reader = _reader(_Transport(handler))
    records = reader.query_traffic(_entity("Service", "payment-service"), query)
    assert observed[0]["step"][0] == "117"
    assert records[0].at < T0 < records[-1].at
    assert records[0].metric == "http_requests_per_second"
    again = reader.query_traffic(_entity("Service", "payment-service"), query)
    assert tuple(item.evidence_id for item in records) == tuple(item.evidence_id for item in again)


def test_live_support_passive_invariants_and_backend_composition() -> None:
    empty = LiveSource(
        incident="empty",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
    )
    assert not empty.supports("resource_pressure")
    assert not empty.supports("traffic")
    assert empty.resource_pressure((_entity(),), T0) == []
    assert empty.traffic_observations() == []

    reader = _reader(_Transport(lambda url, headers: _Response(_success([]))))
    configured = LiveSource(
        incident="configured",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
        prometheus_reader=reader,
    )
    assert configured.supports("resource_pressure")
    assert configured.supports("traffic")
    backend = configured.investigation_backend()
    assert backend.supports("resource_pressure")
    assert backend.supports("traffic")
    assert configured.resource_pressure((_entity(),), T0) == []
    assert configured.traffic_observations() == []


def test_cutoff_clamps_and_prevents_queries_after_cutoff() -> None:
    calls: list[str] = []

    def handler(url: str, headers: dict[str, str]) -> _Response:
        del headers
        calls.append(url)
        return _Response(_success([]))

    reader = _reader(_Transport(handler))
    backend = PrometheusInvestigationBackend(
        base=SourceInvestigationBackend(InMemorySource(name="base")),
        prometheus=reader,
        observation_cutoff=T0,
    )
    backend.query_traffic(
        _entity("Service"), _query(start=T0 - timedelta(minutes=1), end=T0 + timedelta(minutes=1))
    )
    params = parse_qs(urlsplit(calls[0]).query)
    assert int(params["end"][0]) == int(T0.timestamp())
    assert (
        backend.query_traffic(
            _entity("Service"),
            _query(start=T0 + timedelta(seconds=1), end=T0 + timedelta(minutes=1)),
        )
        == ()
    )
    assert len(calls) == 1


def test_source_supports_contract_and_snapshot_fallback() -> None:
    class SourceWithSupport(InMemorySource):
        def supports(self, capability: str) -> bool:
            return capability not in {"resource_pressure", "traffic"}

    source = SourceWithSupport(name="explicit")
    backend = SourceInvestigationBackend(source)
    assert not backend.supports("resource_pressure")
    assert not backend.supports("traffic")
    assert backend.supports("history")

    snapshot_like = InMemorySource(name="snapshot")
    fallback = SourceInvestigationBackend(snapshot_like)
    assert fallback.supports("resource_pressure")
    assert fallback.supports("traffic")
    assert fallback.supports("runtime_traces")


def _gap(capability: str, dimension: GapDimension, target: EntityRef) -> InformationGap:
    return InformationGap(
        gap_id=f"gap:{capability}",
        dimension=dimension,
        missing_fact=capability,
        authorized_queries=(AuthorizedQuery(capability=capability, target=target),),
        candidate_tools=(capability,),
        entity_scope=(target,),
        resolvability=GapResolvability.RESOLVABLE,
    )


class _Acquisition(TypedDict):
    acquired_evidence_refs: tuple[str, ...]
    pending_new_evidence_refs: tuple[str, ...]


def _run_provider_graph(
    source: InMemorySource,
    target: EntityRef,
    gap: InformationGap,
    action: InvestigationAction,
    backend: PrometheusInvestigationBackend,
    tool: InvestigationTool,
) -> tuple[_Acquisition, Case]:
    bounded = initial_view(source)
    base_case = build_case(bounded)
    state = build_investigation_state(bounded, initial_case=base_case)
    state["current_diagnosis"] = state["current_diagnosis"].model_copy(
        update={"information_gaps": (gap,)}
    )
    state["pending_action"] = action
    runtime = _Runtime(
        source=bounded,
        policy=ScriptedInvestigationPolicy([]),
        tools={action.capability or "": tool},
        config=InvestigationConfig(),
        initial_case=base_case,
        rebuild_case=None,
        backend=backend,
        evidence_store=InMemoryEvidenceStore(),
    )
    observed = _execute_tool(state, runtime)
    state["pending_observation"] = observed["pending_observation"]
    acquired = cast(_Acquisition, _check_novelty(state, runtime))
    rebuilt = runtime.case_for(acquired["acquired_evidence_refs"], ())
    return acquired, rebuilt


def test_resource_and_traffic_provider_graphs_rebuild_existing_findings() -> None:
    pod = _entity()
    resource_transport = _Transport(
        lambda url, headers: _Response(
            _success([_series([_sample(T0, "0.20"), _sample(T0 + timedelta(seconds=15), "0.95")])])
        )
    )
    resource_reader = _reader(resource_transport)
    resource_backend = PrometheusInvestigationBackend(
        base=SourceInvestigationBackend(InMemorySource(name="resource")),
        prometheus=resource_reader,
        observation_cutoff=T0 + timedelta(minutes=5),
    )
    resource_source = InMemorySource(
        name="resource",
        alert_items=[
            Alert(
                name="ContainerPressure",
                service=pod.name,
                namespace=pod.namespace,
                starts_at=T0,
                labels={"pod": pod.name, "namespace": pod.namespace},
            )
        ],
        versions=[
            ObjectVersion(
                entity=pod,
                observed_at=T0,
                body={
                    "kind": "Pod",
                    "metadata": {"name": pod.name, "namespace": pod.namespace},
                    "spec": {"containers": [{"name": "app"}]},
                },
                evidence_id="object:pod:initial",
            )
        ],
    )
    resource_gap = _gap("resource_pressure", GapDimension.RESOURCE_PRESSURE, pod)
    resource_action = InvestigationAction(
        action="inspect",
        gap_id=resource_gap.gap_id,
        capability="resource_pressure",
        target=pod,
        query=_query(),
    )
    acquired, resource_case = _run_provider_graph(
        resource_source,
        pod,
        resource_gap,
        resource_action,
        resource_backend,
        ResourcePressureTool(resource_backend),
    )
    assert acquired["pending_new_evidence_refs"]
    assert resource_case.source.resource_pressure((pod,), T0)
    assert any(item.kind is FindingKind.RESOURCE_PRESSURE for item in resource_case.findings)
    pressure_finding = next(
        item for item in resource_case.findings if item.kind is FindingKind.RESOURCE_PRESSURE
    )
    assert pressure_finding.evidence_ids[0].startswith("prometheus:resource:")

    service = _entity("Service", "payment-service")
    traffic_transport = _Transport(
        lambda url, headers: _Response(
            _success(
                [
                    {
                        "metric": {},
                        "values": [
                            _sample(T0 - timedelta(seconds=1), "10"),
                            _sample(T0, "10"),
                            _sample(T0 + timedelta(seconds=1), "20"),
                        ],
                    }
                ]
            )
        )
    )
    traffic_reader = _reader(traffic_transport)
    traffic_backend = PrometheusInvestigationBackend(
        base=SourceInvestigationBackend(InMemorySource(name="traffic")),
        prometheus=traffic_reader,
        observation_cutoff=T0 + timedelta(minutes=5),
    )
    traffic_source = InMemorySource(
        name="traffic",
        alert_items=[Alert(name="RequestErrorRate", service=service.name, starts_at=T0)],
    )
    traffic_gap = _gap("traffic", GapDimension.METRIC_CHANGE, service)
    traffic_action = InvestigationAction(
        action="inspect",
        gap_id=traffic_gap.gap_id,
        capability="traffic",
        target=service,
        query=_query(),
    )
    traffic_acquired, traffic_case = _run_provider_graph(
        traffic_source,
        service,
        traffic_gap,
        traffic_action,
        traffic_backend,
        TrafficTool(traffic_backend),
    )
    assert traffic_acquired["pending_new_evidence_refs"]
    assert any(item.kind is FindingKind.TRAFFIC_INCREASE for item in traffic_case.findings)
    traffic_finding = next(
        item for item in traffic_case.findings if item.kind is FindingKind.TRAFFIC_INCREASE
    )
    assert traffic_finding.evidence_ids[0].startswith("prometheus:traffic:")


def test_provider_evidence_is_not_thresholded_and_no_data_is_safe() -> None:
    steady = _Transport(
        lambda url, headers: _Response(
            _success([_series([_sample(T0, "0.70"), _sample(T0 + timedelta(seconds=15), "0.75")])])
        )
    )
    reader = _reader(steady)
    records = reader.query_resource_pressure(_entity(), _query())
    assert records
    assert records[0].peak == 0.75

    no_data = _Transport(lambda url, headers: _Response(_success([])))
    assert _reader(no_data).query_traffic(_entity("Service"), _query()) == ()


def test_resource_runtime_status_distinguishes_no_data_normal_and_abnormal() -> None:
    pod = _entity()
    source = InMemorySource(
        name="resource-runtime-state",
        alert_items=[Alert(name="PodPressure", service="payment-service", starts_at=T0)],
    )
    case = build_case(source)
    gap = _gap("resource_pressure", GapDimension.RESOURCE_PRESSURE, pod)
    query = InvestigationQuery(start=T0 - timedelta(seconds=15), end=T0, limit=32)

    def observe(
        values: list[list[object]], read_query: InvestigationQuery = query
    ) -> InvestigationObservation:
        transport = _Transport(
            lambda url, headers: _Response(_success([_series(values)]) if values else _success([]))
        )
        reader = _reader(transport)
        backend = PrometheusInvestigationBackend(
            base=SourceInvestigationBackend(source),
            prometheus=reader,
            observation_cutoff=T0 + timedelta(minutes=5),
        )
        return ResourcePressureTool(backend).execute_query(case, gap, pod, read_query)

    normal = observe([_sample(T0 - timedelta(seconds=15), "0.30"), _sample(T0, "0.40")])
    assert normal.runtime is not None
    assert normal.runtime.state is RuntimeObservationState.OBSERVED_NORMAL
    assert normal.runtime.query.template_id == "prometheus.resource_pressure.v1"
    assert normal.runtime.query.target == pod
    assert len(normal.runtime.source_observation_ids) == 2
    assert all(item["sample_count"] == 2 for item in normal.payload["resource_pressure"])
    assert all(item["sample_start"] for item in normal.payload["resource_pressure"])
    assert normalize_observation(normal, case=case, gap=gap).findings == ()

    abnormal = observe([_sample(T0 - timedelta(seconds=15), "0.20"), _sample(T0, "0.95")])
    assert abnormal.runtime is not None
    assert abnormal.runtime.state is RuntimeObservationState.OBSERVED_ABNORMAL
    normalized = normalize_observation(abnormal, case=case, gap=gap)
    assert any(finding.kind is FindingKind.RESOURCE_PRESSURE for finding in normalized.findings)
    pressure_finding = next(
        finding for finding in normalized.findings if finding.kind is FindingKind.RESOURCE_PRESSURE
    )
    assert pressure_finding.details["runtime_pillar"] == "PROMETHEUS"
    assert pressure_finding.details["runtime_target"] == pod.canonical
    assert pressure_finding.details["runtime_query_descriptor_id"].startswith("sha256:")
    assert pressure_finding.details["runtime_normalization_rule_id"] == (
        "prometheus.resource_pressure_threshold.v1"
    )

    partial = observe(
        [_sample(T0 - timedelta(seconds=15), "0.30"), _sample(T0, "0.40")],
        _query(),
    )
    assert partial.runtime is not None
    assert partial.runtime.state is RuntimeObservationState.UNKNOWN

    no_data = observe([])
    assert no_data.runtime is not None
    assert no_data.runtime.state is RuntimeObservationState.NO_DATA
    assert no_data.runtime.source_observation_ids == ()
    assert normalize_observation(no_data, case=case, gap=gap).findings == ()
