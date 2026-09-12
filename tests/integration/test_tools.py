"""Bounded read-only investigation tool tests."""

import time
from uuid import uuid4

from packages.tools import (
    BoundedToolExecutor,
    InMemoryToolAuditSink,
    ToolErrorCode,
    ToolFailure,
    ToolRequest,
    kubernetes_read_tool,
    metrics_tool,
)
from packages.tools.live_backends import LokiBackend, PrometheusBackend, TempoBackend


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

    import pytest

    with pytest.raises(ValueError, match="service is invalid"):
        prometheus.query("service_latency", {})
    with pytest.raises(ValueError, match="consumer is invalid"):
        prometheus.query("kafka_consumer_lag", {})
    with pytest.raises(ValueError, match="service is invalid"):
        loki.query("query_logs", {})
    with pytest.raises(ValueError, match="service is invalid"):
        tempo.query("search_traces", {})
