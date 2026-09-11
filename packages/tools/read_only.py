"""Deterministic bounded metric, log, trace, and Kubernetes read tools."""

from collections.abc import Callable
from typing import Any

from packages.tools.contracts import ToolRequest


class BackendReadTool:
    """Generic read-only adapter with an explicit operation allow-list."""

    def __init__(
        self,
        name: str,
        version: str,
        allowed_operations: frozenset[str],
        backend: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.name = name
        self.version = version
        self._allowed_operations = allowed_operations
        self._backend = backend

    def run(self, request: ToolRequest) -> dict[str, Any]:
        operation = request.parameters.get("operation")
        if not isinstance(operation, str) or operation not in self._allowed_operations:
            raise PermissionError("operation is not allowed by read-only tool policy")
        return self._backend(operation, request.parameters)


def metrics_tool(backend: Callable[[str, dict[str, Any]], dict[str, Any]]) -> BackendReadTool:
    """Create a bounded metrics tool with named query templates only."""
    return BackendReadTool(
        "metrics",
        "1",
        frozenset(
            {
                "service_error_rate",
                "service_latency",
                "db_connection_pressure",
                "kafka_consumer_lag",
            }
        ),
        backend,
    )


def logs_tool(backend: Callable[[str, dict[str, Any]], dict[str, Any]]) -> BackendReadTool:
    """Create a bounded structured-log tool."""
    return BackendReadTool("logs", "1", frozenset({"query_logs", "find_log_patterns"}), backend)


def traces_tool(backend: Callable[[str, dict[str, Any]], dict[str, Any]]) -> BackendReadTool:
    """Create a bounded trace inspection tool."""
    return BackendReadTool("traces", "1", frozenset({"search_traces", "get_trace"}), backend)


def kubernetes_read_tool(
    backend: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> BackendReadTool:
    """Create a Kubernetes read-only tool; write verbs are absent by construction."""
    return BackendReadTool(
        "kubernetes",
        "1",
        frozenset(
            {
                "get_pods",
                "get_deployment",
                "get_events",
                "get_rollout_history",
                "get_container_restarts",
                "get_resource_state",
            }
        ),
        backend,
    )
