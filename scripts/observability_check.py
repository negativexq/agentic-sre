"""Live H0-H6 and H11-H14 checks for a port-forwarded kind runtime."""

from __future__ import annotations

import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from packages.tools import (
    BoundedToolExecutor,
    InMemoryToolAuditSink,
    LokiBackend,
    PrometheusBackend,
    TempoBackend,
    ToolRequest,
    ToolResponse,
    logs_tool,
    metrics_tool,
    traces_tool,
)


def get_json(base_url: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    try:
        with urlopen(f"{base_url.rstrip('/')}{path}{query}", timeout=5) as response:
            payload = json.loads(response.read(10_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"request failed: {base_url}{path}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("backend returned a non-object response")
    return payload


def wait_for(predicate: object, description: str, timeout: int = 35) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate):
            try:
                if predicate():
                    return
            except (ConnectionError, RuntimeError, ValueError):
                pass
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}")


def main() -> int:
    prometheus_url = "http://localhost:19090"
    loki_url = "http://localhost:19300"
    tempo_url = "http://localhost:19320"
    alertmanager_url = "http://localhost:19093"
    grafana_url = "http://localhost:13000"
    order_url = "http://localhost:18000"

    assert "success" == get_json(prometheus_url, "/api/v1/query", {"query": "up"}).get("status")
    assert get_json(loki_url, "/loki/api/v1/labels").get("status") == "success"
    assert get_json(tempo_url, "/api/search", {"limit": "1"}).get("traces") is not None
    assert get_json(alertmanager_url, "/api/v2/status").get("cluster") is not None
    assert get_json(grafana_url, "/api/health").get("database") == "ok"

    request_id = "REQ-LIVE-CORRELATION"
    request = Request(
        f"{order_url}/orders",
        data=json.dumps(
            {"customer_id": "live-check", "amount_cents": 1250, "currency": "USD"}
        ).encode(),
        headers={"content-type": "application/json", "x-request-id": request_id},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
            traceparent = response.headers.get("traceparent", "")
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"deterministic correlation request failed: {error}") from error
    parts = traceparent.split("-")
    if len(parts) != 4 or len(parts[1]) != 32:
        raise RuntimeError("response did not expose a W3C trace ID")
    trace_id = parts[1]

    prometheus = PrometheusBackend(prometheus_url)
    loki = LokiBackend(loki_url)
    tempo = TempoBackend(tempo_url)
    sink = InMemoryToolAuditSink()
    executor = BoundedToolExecutor(sink)
    incident_id = uuid4()

    metrics_request = ToolRequest(
        tool_name="metrics",
        tool_version="1",
        incident_id=incident_id,
        timeout_ms=5_000,
        max_results=100,
        max_bytes=1_000_000,
        parameters={"operation": "service_latency", "service": "payment-service"},
    )
    logs_request = ToolRequest(
        tool_name="logs",
        tool_version="1",
        incident_id=incident_id,
        timeout_ms=5_000,
        max_results=100,
        max_bytes=1_000_000,
        parameters={
            "operation": "query_logs",
            "service": "order-service",
            "request_id": request_id,
        },
    )
    traces_request = ToolRequest(
        tool_name="traces",
        tool_version="1",
        incident_id=incident_id,
        timeout_ms=5_000,
        max_results=20,
        max_bytes=5_000_000,
        parameters={"operation": "get_trace", "trace_id": trace_id},
    )
    wait_for(
        lambda: bool(
            LokiBackend(loki_url).query(
                "query_logs",
                {"service": "order-service", "request_id": request_id},
            )["records"]
        ),
        "correlated log in Loki",
    )
    wait_for(
        lambda: bool(TempoBackend(tempo_url).query("get_trace", {"trace_id": trace_id})["records"]),
        "correlated trace in Tempo",
    )
    results = [
        executor.execute(metrics_tool(prometheus.query), metrics_request),
        executor.execute(logs_tool(loki.query), logs_request),
        executor.execute(traces_tool(tempo.query), traces_request),
    ]
    response_results: list[ToolResponse] = []
    for result in results:
        if not isinstance(result, ToolResponse):
            raise RuntimeError(f"live tool failure: {result}")
        response_results.append(result)
    if not any(request_id in json.dumps(result.data) for result in response_results):
        raise RuntimeError("correlation request ID was not found in live tool output")
    if len(sink.records) != 3:
        raise RuntimeError("live tool audit count is not three")
    print("live observability checks: PASS")
    print(f"trace_id={trace_id} tool_calls={len(sink.records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
