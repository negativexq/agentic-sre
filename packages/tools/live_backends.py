"""HTTP read-only adapters for the live observability backends.

The adapters expose named operations only.  Callers cannot submit arbitrary
PromQL or LogQL through this boundary; each operation owns its query template
and applies a bounded time range and result limit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LiveBackend:
    """Small bounded JSON HTTP client for a read-only backend."""

    def __init__(self, base_url: str, *, max_timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_timeout_seconds = max_timeout_seconds

    def _get(self, path: str, params: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
        timeout = min(max(timeout_seconds, 0.001), self._max_timeout_seconds)
        query = f"?{urlencode(params)}" if params else ""
        request = Request(f"{self._base_url}{path}{query}", method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(10_000_001))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ConnectionError(f"live backend request failed: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("live backend returned a non-object response")
        if payload.get("status") == "error":
            raise ValueError(str(payload.get("error", "backend query failed")))
        return payload


def _time_window(parameters: dict[str, Any], *, maximum_seconds: int = 900) -> tuple[str, str]:
    seconds = int(parameters.get("range_seconds", 300))
    if seconds <= 0 or seconds > maximum_seconds:
        raise ValueError(f"range_seconds must be between 1 and {maximum_seconds}")
    end = datetime.now(UTC)
    return str(int((end - timedelta(seconds=seconds)).timestamp() * 1_000_000_000)), str(
        int(end.timestamp() * 1_000_000_000)
    )


def _service(parameters: dict[str, Any]) -> str:
    service = parameters.get("service", "payment-service")
    if not isinstance(service, str) or not service or len(service) > 80:
        raise ValueError("service is invalid")
    return service


class PrometheusBackend(LiveBackend):
    """Named, bounded Prometheus query templates."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        service = _service(parameters)
        queries = {
            "service_error_rate": (
                f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[30s]))'
            ),
            "service_latency": (
                f'sum(rate(http_request_duration_seconds_sum{{service="{service}"}}[30s])) '
                f'/ sum(rate(http_request_duration_seconds_count{{service="{service}"}}[30s]))'
            ),
            "db_connection_pressure": "sum(db_connection_acquisition_seconds_count)",
            "kafka_consumer_lag": (
                f'kafka_consumer_lag{{service="{parameters.get("consumer", "order-worker")}"}}'
            ),
        }
        if operation not in queries:
            raise ValueError("unsupported Prometheus operation")
        payload = self._get("/api/v1/query", {"query": queries[operation]}, 5.0)
        result = payload.get("data", {}).get("result", [])
        if not isinstance(result, list):
            raise ValueError("Prometheus result is invalid")
        return {"backend": "prometheus", "operation": operation, "records": result[:100]}


class LokiBackend(LiveBackend):
    """Bounded structured-log queries against Loki."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"query_logs", "find_log_patterns"}:
            raise ValueError("unsupported Loki operation")
        start, end = _time_window(parameters)
        service = _service(parameters)
        query = f'{{service_name="{service}"}}'
        pattern = parameters.get("pattern")
        request_id = parameters.get("request_id")
        if operation == "find_log_patterns" and pattern:
            if not isinstance(pattern, str) or len(pattern) > 100:
                raise ValueError("pattern is invalid")
            query += f' |= "{pattern}"'
        if request_id:
            if not isinstance(request_id, str) or len(request_id) > 100:
                raise ValueError("request_id is invalid")
            query += f' | request_id = "{request_id}"'
        payload = self._get(
            "/loki/api/v1/query_range",
            {"query": query, "start": start, "end": end, "limit": "100"},
            5.0,
        )
        streams = payload.get("data", {}).get("result", [])
        records: list[dict[str, Any]] = []
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                labels = stream.get("stream", {})
                for entry in stream.get("values", []):
                    if isinstance(entry, list) and len(entry) >= 2:
                        records.append({"labels": labels, "timestamp": entry[0], "line": entry[1]})
        return {"backend": "loki", "operation": operation, "records": records[:100]}


class TempoBackend(LiveBackend):
    """Bounded trace search and retrieval against Tempo."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation == "search_traces":
            service = _service(parameters)
            payload = self._get(
                "/api/search",
                {"limit": "20", "tags": f"service.name={service}"},
                5.0,
            )
            traces = payload.get("traces", [])
            if not isinstance(traces, list):
                raise ValueError("Tempo search result is invalid")
            return {"backend": "tempo", "operation": operation, "records": traces[:20]}
        if operation == "get_trace":
            trace_id = parameters.get("trace_id")
            if not isinstance(trace_id, str) or len(trace_id) != 32:
                raise ValueError("trace_id must be a 32-character hexadecimal ID")
            try:
                int(trace_id, 16)
            except ValueError as error:
                raise ValueError("trace_id must be hexadecimal") from error
            payload = self._get(f"/api/traces/{trace_id}", {}, 5.0)
            return {"backend": "tempo", "operation": operation, "records": [payload]}
        raise ValueError("unsupported Tempo operation")
