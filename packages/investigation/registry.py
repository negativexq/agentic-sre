"""Allow-listed read-only investigation tool registry."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from packages.contracts import EvidenceSourceType
from packages.tools import (
    KubernetesBackend,
    KubernetesChangeBackend,
    LokiBackend,
    PrometheusBackend,
    TempoBackend,
    ToolRequest,
    change_read_tool,
    kubernetes_read_tool,
)
from packages.tools.executor import Tool


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """Named operation exposed to the model, backed by a bounded tool."""

    name: str
    version: str
    operation: str
    source_type: EvidenceSourceType
    tool: Tool
    purpose: str = ""
    argument_schema: dict[str, Any] = field(default_factory=dict)

    def descriptor(self) -> dict[str, Any]:
        """Return the safe model-facing capability descriptor."""
        return {
            "name": self.name,
            "version": self.version,
            "purpose": self.purpose or f"Read-only {self.operation} observation",
            "evidence_type": self.source_type.value,
            "arguments": self.argument_schema,
        }

    def request(self, incident_id: UUID, arguments: dict[str, Any]) -> ToolRequest:
        """Build a bounded invocation while preventing operation override."""
        parameters = {**arguments, "operation": self.operation}
        return ToolRequest(
            tool_name=self.tool.name,
            tool_version=self.tool.version,
            incident_id=incident_id,
            tool_call_id=uuid4(),
            timeout_ms=5_000,
            max_results=100,
            max_bytes=100_000,
            parameters=parameters,
        )


class ReadOnlyToolRegistry:
    """Registry that has no write or arbitrary execution registration path."""

    def __init__(self, tools: tuple[RegisteredTool, ...] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: RegisteredTool) -> None:
        """Register one named tool and reject write-like names fail-closed."""
        forbidden = {"kubectl", "bash", "shell", "restart", "rollback", "scale", "run_command"}
        if tool.name.lower() in forbidden or tool.name in self._tools:
            raise ValueError("tool is not permitted in the read-only registry")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool:
        """Resolve a model name or reject it before backend execution."""
        try:
            return self._tools[name]
        except KeyError as error:
            raise PermissionError("tool is not registered in the read-only registry") from error

    def names(self) -> tuple[str, ...]:
        """Return stable tool names for compact context construction."""
        return tuple(sorted(self._tools))

    def descriptors(self) -> tuple[dict[str, Any], ...]:
        """Return deterministic bounded capability descriptors."""
        return tuple(self._tools[name].descriptor() for name in sorted(self._tools))


def live_observability_registry(
    prometheus_url: str,
    loki_url: str,
    tempo_url: str,
) -> ReadOnlyToolRegistry:
    """Build the named live registry from bounded backend adapters only."""
    prometheus = PrometheusBackend(prometheus_url)
    loki = LokiBackend(loki_url)
    tempo = TempoBackend(tempo_url)
    from packages.tools import logs_tool, metrics_tool, traces_tool

    metrics = metrics_tool(prometheus.query)
    logs = logs_tool(loki.query)
    traces = traces_tool(tempo.query)
    kubernetes = KubernetesBackend()
    k8s_tool = kubernetes_read_tool(kubernetes.query)
    changes = change_read_tool(KubernetesChangeBackend(kubernetes).query)
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                "service_error_rate",
                "1",
                "service_error_rate",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded HTTP error rate for an explicitly named service.",
                {"service": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "service_latency",
                "1",
                "service_latency",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded HTTP latency for an explicitly named service.",
                {"service": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "db_connection_pressure",
                "1",
                "db_connection_pressure",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure database connection acquisition pressure.",
                {"service": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kafka_consumer_lag",
                "1",
                "kafka_consumer_lag",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure Kafka lag for an explicitly named consumer.",
                {"consumer": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "db_query_latency",
                "1",
                "db_query_latency",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded database query latency for an explicitly named service.",
                {"service": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "service_logs",
                "1",
                "query_logs",
                EvidenceSourceType.LOG,
                logs,
                "Search bounded structured logs for an explicitly named service.",
                {
                    "service": {"type": "string", "required": True},
                    "range_seconds": {"type": "integer", "required": False},
                },
            ),
            RegisteredTool(
                "service_error_logs",
                "1",
                "find_log_patterns",
                EvidenceSourceType.LOG,
                logs,
                "Find bounded error patterns in an explicitly named service's logs.",
                {
                    "service": {"type": "string", "required": True},
                    "pattern": {"type": "string", "required": False},
                },
            ),
            RegisteredTool(
                "slow_traces",
                "1",
                "search_traces",
                EvidenceSourceType.TRACE,
                traces,
                "Search bounded slow traces for an explicitly named service.",
                {"service": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "trace_detail",
                "1",
                "get_trace",
                EvidenceSourceType.TRACE,
                traces,
                "Retrieve one bounded trace by its trace identifier.",
                {"trace_id": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_pods",
                "1",
                "get_pods",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded pod health and restart state for a named deployment.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_deployment",
                "1",
                "get_deployment",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded deployment replica and image state.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_events",
                "1",
                "get_events",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded Kubernetes events for a named deployment.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_rollout_history",
                "1",
                "get_rollout_history",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded deployment revision metadata.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_container_restarts",
                "1",
                "get_container_restarts",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded container restart counts for a named deployment.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "kubernetes_resource_state",
                "1",
                "get_resource_state",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded current deployment resource state.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "recent_deployment_changes",
                "1",
                "recent_deployment_changes",
                EvidenceSourceType.CHANGE,
                changes,
                "Inspect bounded observable deployment revision and image facts.",
                {"deployment": {"type": "string", "required": True}},
            ),
            RegisteredTool(
                "recent_configuration_changes",
                "1",
                "recent_configuration_changes",
                EvidenceSourceType.CHANGE,
                changes,
                "Inspect bounded observable deployment configuration facts.",
                {"deployment": {"type": "string", "required": True}},
            ),
        )
    )
