"""Bounded read-only investigation tool tests."""

import time
from io import BytesIO
from typing import Any, cast
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest

from packages.tools import (
    BackendProtocolError,
    BoundedToolExecutor,
    InMemoryToolAuditSink,
    ToolErrorCode,
    ToolFailure,
    ToolRequest,
    kubernetes_read_tool,
    metrics_tool,
)
from packages.tools.live_backends import (
    LokiBackend,
    PrometheusBackend,
    TempoBackend,
    _loki_time_params,
    _prometheus_time_params,
    _tempo_time_params,
)


def make_request(tool_name: str, **parameters: object) -> ToolRequest:
    return ToolRequest(
        tool_name=tool_name,
        tool_version="1",
        incident_id=uuid4(),
        timeout_ms=100,
        max_results=2,
        max_bytes=500,
        parameters=parameters,
    )


def test_tool_result_bounds_and_audit() -> None:
    audit = InMemoryToolAuditSink()
    tool = metrics_tool(
        lambda _operation, _parameters: {"records": [{"value": 1}, {"value": 2}, {"value": 3}]}
    )
    result = BoundedToolExecutor(audit).execute(
        tool, make_request("metrics", operation="service_error_rate")
    )

    assert isinstance(result, ToolFailure)
    assert result.code is ToolErrorCode.RESULT_LIMIT_EXCEEDED
    assert len(audit.records) == 1


def test_timeout_and_invalid_read_operation_are_typed() -> None:
    def slow_backend(_operation: str, _parameters: dict[str, object]) -> dict[str, object]:
        time.sleep(0.05)
        return {"records": []}

    slow = metrics_tool(slow_backend)
    timeout_request = make_request("metrics", operation="service_latency").model_copy(
        update={"timeout_ms": 1}
    )
    timeout = BoundedToolExecutor().execute(slow, timeout_request)
    assert isinstance(timeout, ToolFailure)
    assert timeout.code is ToolErrorCode.TOOL_TIMEOUT

    invalid = BoundedToolExecutor().execute(
        metrics_tool(lambda _operation, _parameters: {}),
        make_request("metrics", operation="raw_promql"),
    )
    assert isinstance(invalid, ToolFailure)
    assert invalid.code is ToolErrorCode.PERMISSION_DENIED


def test_kubernetes_tool_is_read_only() -> None:
    tool = kubernetes_read_tool(lambda operation, _parameters: {"operation": operation})
    read_result = BoundedToolExecutor().execute(
        tool, make_request("kubernetes", operation="get_pods")
    )
    write_result = BoundedToolExecutor().execute(
        tool, make_request("kubernetes", operation="delete_pod")
    )

    assert not isinstance(read_result, ToolFailure)
    assert isinstance(write_result, ToolFailure)
    assert write_result.code is ToolErrorCode.PERMISSION_DENIED


def test_live_backends_reject_implicit_service_or_consumer_scope() -> None:
    """Agent-directed live queries cannot silently change investigation scope."""
    prometheus = PrometheusBackend("http://127.0.0.1:1")
    loki = LokiBackend("http://127.0.0.1:1")
    tempo = TempoBackend("http://127.0.0.1:1")

    with pytest.raises(ValueError, match="service is invalid"):
        prometheus.query("service_latency", {})
    with pytest.raises(ValueError, match="consumer is invalid"):
        prometheus.query("kafka_consumer_lag", {})
    with pytest.raises(ValueError, match="service is invalid"):
        loki.query("query_logs", {})
    with pytest.raises(ValueError, match="service is invalid"):
        tempo.query("search_traces", {})


def test_backend_time_serializers_use_backend_specific_units() -> None:
    """The semantic window has distinct Prometheus, Loki, and Tempo encodings."""
    parameters = {
        "observation_window": {
            "starts_at": "2026-09-12T12:00:00+00:00",
            "ends_at": "2026-09-12T12:05:00+00:00",
        }
    }
    prometheus, _ = _prometheus_time_params(parameters)
    loki, _ = _loki_time_params(parameters)
    tempo, _ = _tempo_time_params(parameters)
    assert 10**9 < float(prometheus["start"]) < 10**11
    assert 10**18 < int(loki["start"]) < 10**19
    assert 10**9 < float(tempo["start"]) < 10**11


def test_backend_http_failures_have_typed_classes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Reachability, request, server, timeout, and response failures stay distinct."""
    from packages.tools.live_backends import LiveBackend

    backend = LiveBackend("http://backend")

    def invoke(error: BaseException) -> BackendProtocolError:
        def failing(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise error

        monkeypatch.setattr("packages.tools.live_backends.urlopen", failing)
        with pytest.raises(BackendProtocolError) as raised:
            backend._get("/api", {}, 1, backend="test", operation="query")
        return raised.value

    assert (
        invoke(
            HTTPError("http://backend/api", 400, "bad", cast(Any, {}), BytesIO(b"{}"))
        ).code.value
        == "BACKEND_REQUEST_REJECTED"
    )
    assert (
        invoke(
            HTTPError("http://backend/api", 500, "bad", cast(Any, {}), BytesIO(b"{}"))
        ).code.value
        == "BACKEND_SERVER_ERROR"
    )
    assert invoke(URLError("refused")).code.value == "BACKEND_UNAVAILABLE"
    assert invoke(TimeoutError()).code.value == "BACKEND_TIMEOUT"


def test_prometheus_kafka_request_uses_seconds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The generated query_range request cannot carry nanosecond bounds."""
    captured: dict[str, object] = {}

    def fake_get(self, path, params, timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(path=path, params=params, kwargs=kwargs)
        return {"data": {"result": []}}

    monkeypatch.setattr(PrometheusBackend, "_get", fake_get)
    PrometheusBackend("http://prometheus").query(
        "kafka_consumer_lag",
        {
            "consumer": "order-worker",
            "observation_window": {
                "starts_at": "2026-09-12T12:00:00+00:00",
                "ends_at": "2026-09-12T12:05:00+00:00",
            },
        },
    )
    assert captured["path"] == "/api/v1/query_range"
    params = captured["params"]
    assert isinstance(params, dict)
    assert float(params["start"]) < 10**11
    query = str(params["query"])
    assert (
        'kafka_messages_total{service="order-service",topic="orders.created",direction="produced"}'
        in query
    )
    assert (
        'kafka_messages_total{service="order-worker",topic="orders.created",direction="consumed"}'
        in query
    )
    assert "clamp_min" in query


def test_database_queries_are_service_scoped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Database evidence must not aggregate another service's signal."""
    captured: list[dict[str, object]] = []

    def fake_get(self, path, params, timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(params)
        return {"data": {"result": []}}

    monkeypatch.setattr(PrometheusBackend, "_get", fake_get)
    backend = PrometheusBackend("http://prometheus")
    for operation, service in (
        ("db_connection_pressure", "payment-service"),
        ("db_query_latency", "order-service"),
    ):
        backend.query(operation, {"service": service, "range_seconds": 60})

    assert 'service="payment-service"' in str(captured[0]["query"])
    assert 'service="order-service"' in str(captured[1]["query"])
    assert "db_connection_acquisition_seconds_count" in str(captured[0]["query"])
    assert 'db_query_duration_seconds_count{service="order-service",operation="create"}' in str(
        captured[1]["query"]
    )


def test_service_latency_excludes_infrastructure_routes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Latency evidence measures workload traffic, not health/scrape probes."""
    captured: dict[str, object] = {}

    def fake_get(self, path, params, timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(params=params)
        return {"data": {"result": []}}

    monkeypatch.setattr(PrometheusBackend, "_get", fake_get)
    PrometheusBackend("http://prometheus").query(
        "service_latency", {"service": "order-service", "range_seconds": 60}
    )

    params = captured["params"]
    assert isinstance(params, dict)
    query = str(params["query"])
    assert 'route!~"/(health|metrics|__faults).*"' in query


def test_tempo_search_request_uses_integer_seconds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Tempo search receives integer Unix seconds, not Loki nanoseconds."""
    captured: dict[str, object] = {}

    def fake_get(self, path, params, timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(path=path, params=params, kwargs=kwargs)
        return {"traces": []}

    monkeypatch.setattr(TempoBackend, "_get", fake_get)
    TempoBackend("http://tempo").query(
        "search_traces",
        {
            "service": "order-worker",
            "observation_window": {
                "starts_at": "2026-09-12T12:00:00+00:00",
                "ends_at": "2026-09-12T12:05:00+00:00",
            },
        },
    )
    assert captured["path"] == "/api/search"
    params = captured["params"]
    assert isinstance(params, dict)
    assert int(params["start"]) < 10**11
