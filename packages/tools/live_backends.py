"""HTTP read-only adapters for the live observability backends.

The adapters expose named operations only.  Callers cannot submit arbitrary
PromQL or LogQL through this boundary; each operation owns its query template
and applies a bounded time range and result limit.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from packages.contracts import ChangeRecord, ChangeScope
from packages.tools.contracts import BackendProtocolError, ToolErrorCode

MAX_INCIDENT_WINDOW_SECONDS = 900
CHANGE_LOOKBACK_SECONDS = 900
MAX_CHANGE_QUERY_WINDOW_SECONDS = 1_800


class LiveBackend:
    """Small bounded JSON HTTP client for a read-only backend."""

    def __init__(self, base_url: str, *, max_timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_timeout_seconds = max_timeout_seconds

    def _get(
        self,
        path: str,
        params: dict[str, str],
        timeout_seconds: float,
        *,
        backend: str,
        operation: str,
    ) -> dict[str, Any]:
        timeout = min(max(timeout_seconds, 0.001), self._max_timeout_seconds)
        query = f"?{urlencode(params)}" if params else ""
        request = Request(f"{self._base_url}{path}{query}", method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(10_000_001))
        except HTTPError as error:
            code = (
                ToolErrorCode.BACKEND_REQUEST_REJECTED
                if 400 <= error.code < 500
                else ToolErrorCode.BACKEND_SERVER_ERROR
            )
            raise BackendProtocolError(
                code,
                "live backend rejected or failed the request",
                backend=backend,
                operation=operation,
                http_status=error.code,
            ) from error
        except TimeoutError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_TIMEOUT,
                "live backend request timed out",
                backend=backend,
                operation=operation,
            ) from error
        except URLError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_UNAVAILABLE,
                "live backend is unavailable",
                backend=backend,
                operation=operation,
            ) from error
        except json.JSONDecodeError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_RESPONSE_INVALID,
                "live backend returned invalid JSON",
                backend=backend,
                operation=operation,
            ) from error
        if not isinstance(payload, dict):
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_RESPONSE_INVALID,
                "live backend returned a non-object response",
                backend=backend,
                operation=operation,
            )
        if payload.get("status") == "error":
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_REQUEST_REJECTED,
                "live backend rejected the query",
                backend=backend,
                operation=operation,
            )
        return payload


def _parse_observation_window(
    parameters: dict[str, Any], *, maximum_seconds: int = MAX_INCIDENT_WINDOW_SECONDS
) -> tuple[datetime, datetime, dict[str, str]]:
    supplied = parameters.get("observation_window")
    if (
        isinstance(supplied, dict)
        and isinstance(supplied.get("starts_at"), str)
        and isinstance(supplied.get("ends_at"), str)
    ):
        start = datetime.fromisoformat(supplied["starts_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(supplied["ends_at"].replace("Z", "+00:00"))
        if end < start or (end - start).total_seconds() > maximum_seconds:
            raise ValueError("observation_window is outside bounded limits")
        return start, end, {"starts_at": start.isoformat(), "ends_at": end.isoformat()}
    seconds = int(parameters.get("range_seconds", 300))
    if seconds <= 0 or seconds > maximum_seconds:
        raise ValueError(f"range_seconds must be between 1 and {maximum_seconds}")
    end = datetime.now(UTC)
    start = end - timedelta(seconds=seconds)
    return start, end, {"starts_at": start.isoformat(), "ends_at": end.isoformat()}


def _prometheus_time_params(parameters: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Serialize the semantic window as Unix seconds for Prometheus."""
    start, end, window = _parse_observation_window(parameters)
    return {
        "start": f"{start.timestamp():.3f}",
        "end": f"{end.timestamp():.3f}",
        "step": "15",
    }, window


def _loki_time_params(parameters: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Serialize the semantic window as Unix nanoseconds for Loki."""
    start, end, window = _parse_observation_window(parameters)
    return {
        "start": str(int(start.timestamp() * 1_000_000_000)),
        "end": str(int(end.timestamp() * 1_000_000_000)),
    }, window


def _tempo_time_params(parameters: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Serialize the semantic window as Unix seconds for Tempo search."""
    start, end, window = _parse_observation_window(parameters)
    return {"start": str(int(start.timestamp())), "end": str(int(end.timestamp()))}, window


def _change_time_window(
    parameters: dict[str, Any], *, lookback_seconds: int = CHANGE_LOOKBACK_SECONDS
) -> tuple[datetime, datetime, dict[str, str]]:
    """Expand the incident window by one bounded lookback for change evidence."""
    start, end, _ = _parse_observation_window(parameters)
    expanded_start = start - timedelta(seconds=lookback_seconds)
    return (
        expanded_start,
        end,
        {
            "starts_at": expanded_start.isoformat(),
            "ends_at": end.isoformat(),
        },
    )


def _service(parameters: dict[str, Any]) -> str:
    service = parameters.get("service")
    if not isinstance(service, str) or not service or len(service) > 80:
        raise ValueError("service is invalid")
    return service


class PrometheusBackend(LiveBackend):
    """Named, bounded Prometheus query templates."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        service = _service(parameters) if operation != "kafka_consumer_lag" else ""
        queries: dict[str, str] = {
            "service_error_rate": (
                f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[30s]))'
            ),
            "service_latency": (
                f'sum(rate(http_request_duration_seconds_sum{{service="{service}"}}[30s])) '
                f'/ sum(rate(http_request_duration_seconds_count{{service="{service}"}}[30s]))'
            ),
            "db_connection_pressure": "sum(db_connection_acquisition_seconds_count)",
            "db_query_latency": "sum(rate(db_query_duration_seconds_sum[30s])) / sum(rate(db_query_duration_seconds_count[30s]))",
        }
        if operation == "kafka_consumer_lag":
            queries[operation] = f'kafka_consumer_lag{{service="{_consumer(parameters)}"}}'
        if operation not in queries:
            raise ValueError("unsupported Prometheus operation")
        time_params, window = _prometheus_time_params(parameters)
        payload = self._get(
            "/api/v1/query_range",
            {"query": queries[operation], **time_params},
            5.0,
            backend="prometheus",
            operation=operation,
        )
        result = payload.get("data", {}).get("result", [])
        if not isinstance(result, list):
            raise ValueError("Prometheus result is invalid")
        return {
            "backend": "prometheus",
            "operation": operation,
            "records": result[:100],
            "__effective_time_window": window,
            "__temporal_mode": parameters.get("temporal_mode", "INCIDENT_WINDOW"),
        }


def _consumer(parameters: dict[str, Any]) -> str:
    """Require explicit Kafka scope instead of using a hidden worker default."""
    consumer = parameters.get("consumer")
    if not isinstance(consumer, str) or not consumer or len(consumer) > 80:
        raise ValueError("consumer is invalid")
    return consumer


class LokiBackend(LiveBackend):
    """Bounded structured-log queries against Loki."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"query_logs", "find_log_patterns"}:
            raise ValueError("unsupported Loki operation")
        time_params, window = _loki_time_params(parameters)
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
            {"query": query, **time_params, "limit": "100"},
            5.0,
            backend="loki",
            operation=operation,
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
        return {
            "backend": "loki",
            "operation": operation,
            "records": records[:100],
            "__effective_time_window": window,
            "__temporal_mode": parameters.get("temporal_mode", "INCIDENT_WINDOW"),
        }


class TempoBackend(LiveBackend):
    """Bounded trace search and retrieval against Tempo."""

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation == "search_traces":
            service = _service(parameters)
            time_params, window = _tempo_time_params(parameters)
            payload = self._get(
                "/api/search",
                {"limit": "20", "tags": f"service.name={service}", **time_params},
                5.0,
                backend="tempo",
                operation=operation,
            )
            traces = payload.get("traces", [])
            if not isinstance(traces, list):
                raise ValueError("Tempo search result is invalid")
            return {
                "backend": "tempo",
                "operation": operation,
                "records": traces[:20],
                "__effective_time_window": window,
                "__temporal_mode": parameters.get("temporal_mode", "INCIDENT_WINDOW"),
            }
        if operation == "get_trace":
            trace_id = parameters.get("trace_id")
            if not isinstance(trace_id, str) or len(trace_id) != 32:
                raise ValueError("trace_id must be a 32-character hexadecimal ID")
            try:
                int(trace_id, 16)
            except ValueError as error:
                raise ValueError("trace_id must be hexadecimal") from error
            payload = self._get(
                f"/api/traces/{trace_id}",
                {},
                5.0,
                backend="tempo",
                operation=operation,
            )
            _time_params, window = _tempo_time_params(parameters)
            return {
                "backend": "tempo",
                "operation": operation,
                "records": [payload],
                "__effective_time_window": window,
                "__temporal_mode": parameters.get("temporal_mode", "INCIDENT_WINDOW"),
            }
        raise ValueError("unsupported Tempo operation")


class KubernetesBackend:
    """Lazy Kubernetes API adapter exposing read-only, named observations."""

    def __init__(self, namespace: str = "sre-demo") -> None:
        self._namespace = namespace
        self._core: Any | None = None
        self._apps: Any | None = None

    def _clients(self) -> tuple[Any, Any]:
        if self._core is not None and self._apps is not None:
            return self._core, self._apps
        try:
            kubernetes = importlib.import_module("kubernetes")
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_config.load_kube_config()
            self._core = kubernetes.client.CoreV1Api()
            self._apps = kubernetes.client.AppsV1Api()
        except Exception as error:  # pragma: no cover - live cluster only
            raise ConnectionError("Kubernetes read client is unavailable") from error
        return self._core, self._apps

    @staticmethod
    def _deployment_name(parameters: dict[str, Any]) -> str:
        name = parameters.get("deployment") or parameters.get("service")
        if not isinstance(name, str) or not name or len(name) > 80:
            raise ValueError("deployment is required")
        return name

    @staticmethod
    def _annotate(
        payload: dict[str, Any], parameters: dict[str, Any], temporal_mode: str
    ) -> dict[str, Any]:
        """Attach truthful temporal metadata to Kubernetes observations."""
        result = dict(payload)
        window = parameters.get("observation_window")
        if isinstance(window, dict):
            result["__effective_time_window"] = window
        result["__temporal_mode"] = temporal_mode
        return result

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Execute one explicitly allow-listed Kubernetes read operation."""
        core, apps = self._clients()
        name = self._deployment_name(parameters)
        if operation in {"get_pods", "get_container_restarts"}:
            pods = core.list_namespaced_pod(self._namespace, label_selector=f"app={name}").items
            records = [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "restarts": sum(
                        item.restart_count for item in (pod.status.container_statuses or [])
                    ),
                }
                for pod in pods[:50]
            ]
            if operation == "get_container_restarts":
                records = [{"pod": item["name"], "restarts": item["restarts"]} for item in records]
            return self._annotate(
                {"backend": "kubernetes", "operation": operation, "records": records},
                parameters,
                "CURRENT_STATE",
            )
        deployment = apps.read_namespaced_deployment(name, self._namespace)
        if operation == "get_deployment":
            return self._annotate(
                {
                    "backend": "kubernetes",
                    "operation": operation,
                    "records": [
                        {
                            "name": deployment.metadata.name,
                            "generation": deployment.metadata.generation,
                            "replicas": deployment.spec.replicas,
                            "available_replicas": deployment.status.available_replicas or 0,
                            "image": deployment.spec.template.spec.containers[0].image,
                        }
                    ],
                },
                parameters,
                "CURRENT_STATE",
            )
        if operation == "get_rollout_history":
            annotations = deployment.metadata.annotations or {}
            return self._annotate(
                {
                    "backend": "kubernetes",
                    "operation": operation,
                    "records": [
                        {
                            "deployment": name,
                            "revision": annotations.get("deployment.kubernetes.io/revision"),
                            "generation": deployment.metadata.generation,
                        }
                    ],
                },
                parameters,
                "HISTORICAL_CHANGE",
            )
        if operation == "get_resource_state":
            return self._annotate(
                {
                    "backend": "kubernetes",
                    "operation": operation,
                    "records": [
                        {
                            "deployment": name,
                            "desired_replicas": deployment.spec.replicas,
                            "ready_replicas": deployment.status.ready_replicas or 0,
                            "generation": deployment.metadata.generation,
                        }
                    ],
                },
                parameters,
                "CURRENT_STATE",
            )
        if operation == "get_events":
            events = core.list_namespaced_event(
                self._namespace,
                field_selector=f"involvedObject.name={name}",
            ).items
            return self._annotate(
                {
                    "backend": "kubernetes",
                    "operation": operation,
                    "records": [
                        {
                            "reason": event.reason,
                            "message": event.message,
                            "type": event.type,
                            "last_timestamp": str(event.last_timestamp),
                        }
                        for event in events[:100]
                    ],
                },
                parameters,
                "HISTORICAL_EVENT",
            )
        raise ValueError("unsupported Kubernetes operation")


class KubernetesChangeBackend:
    """Expose historical journal facts, with explicit current-state fallback."""

    def __init__(
        self,
        kubernetes: Any,
        historical_reader: Any | None = None,
    ) -> None:
        self._kubernetes = kubernetes
        self.has_historical_source = historical_reader is not None
        self._historical_reader = historical_reader

    def query(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"recent_deployment_changes", "recent_configuration_changes"}:
            raise ValueError("unsupported change operation")
        if self._historical_reader is not None:
            _, _, expanded_window = _change_time_window(parameters)
            reader_parameters = {**parameters, "observation_window": expanded_window}
            records = self._historical_reader(operation, reader_parameters)
            result: dict[str, Any] = {
                "backend": "change_journal",
                "operation": operation,
                "records": [
                    {
                        "change_id": str(record.change_id),
                        "resource_type": record.resource_type,
                        "resource_name": record.resource_name,
                        "change_type": record.change_type.value,
                        "scope": record.scope.value,
                        "timestamp": record.timestamp.isoformat(),
                        "before": record.before,
                        "after": record.after,
                        "revision": record.revision,
                        "source": record.source,
                    }
                    for record in records
                    if isinstance(record, ChangeRecord)
                ],
            }
            result["__effective_time_window"] = expanded_window
            result["__temporal_mode"] = "HISTORICAL_CHANGE"
            return result
        deployment = self._kubernetes.query("get_deployment", parameters)
        current_result: dict[str, Any] = {
            "backend": "kubernetes",
            "operation": operation,
            "records": [
                {
                    "resource_type": "Deployment",
                    "resource_name": parameters.get("deployment") or parameters.get("service"),
                    "change_type": "OBSERVED_CURRENT_STATE",
                    "source": "kubernetes",
                    "facts": deployment.get("records", []),
                }
            ],
        }
        window = parameters.get("observation_window")
        if isinstance(window, dict):
            current_result["__effective_time_window"] = window
        current_result["__temporal_mode"] = "CURRENT_STATE"
        return current_result


class ControlPlaneChangeReader:
    """Read persisted change records through the control-plane API."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint.rstrip("/")

    def query(self, operation: str, parameters: dict[str, Any]) -> list[ChangeRecord]:
        """Fetch only bounded records for the authoritative observation window."""
        if operation not in {"recent_deployment_changes", "recent_configuration_changes"}:
            raise ValueError("unsupported change operation")
        resource_name = parameters.get("deployment")
        if not isinstance(resource_name, str) or not resource_name:
            raise ValueError("deployment is required")
        # The backend owns the lookback policy and passes the expanded window
        # to this reader. The reader only validates and serializes it.
        _, _, window = _parse_observation_window(
            parameters, maximum_seconds=MAX_CHANGE_QUERY_WINDOW_SECONDS
        )
        scope = (
            ChangeScope.DEPLOYMENT
            if operation == "recent_deployment_changes"
            else ChangeScope.CONFIGURATION
        )
        query = urlencode(
            {
                "resource_name": resource_name,
                "starts_at": window["starts_at"],
                "ends_at": window["ends_at"],
                "scope": scope.value,
            }
        )
        request = Request(f"{self._endpoint}?{query}", method="GET")
        try:
            with urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read(1_000_001))
        except HTTPError as error:
            code = (
                ToolErrorCode.BACKEND_REQUEST_REJECTED
                if 400 <= error.code < 500
                else ToolErrorCode.BACKEND_SERVER_ERROR
            )
            raise BackendProtocolError(
                code,
                "change journal rejected or failed the request",
                backend="change_journal",
                operation=operation,
                http_status=error.code,
            ) from error
        except TimeoutError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_TIMEOUT,
                "change journal request timed out",
                backend="change_journal",
                operation=operation,
            ) from error
        except URLError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_UNAVAILABLE,
                "change journal is unavailable",
                backend="change_journal",
                operation=operation,
            ) from error
        except json.JSONDecodeError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_RESPONSE_INVALID,
                "change journal returned invalid JSON",
                backend="change_journal",
                operation=operation,
            ) from error
        if not isinstance(payload, list):
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_RESPONSE_INVALID,
                "change journal returned an invalid record list",
                backend="change_journal",
                operation=operation,
            )
        try:
            return [ChangeRecord.model_validate(item) for item in payload[:100]]
        except ValueError as error:
            raise BackendProtocolError(
                ToolErrorCode.BACKEND_RESPONSE_INVALID,
                "change journal returned an invalid record",
                backend="change_journal",
                operation=operation,
            ) from error
