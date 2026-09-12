"""Allow-listed read-only investigation tool registry."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from packages.contracts import EvidenceSourceType
from packages.tools import LokiBackend, PrometheusBackend, TempoBackend, ToolRequest
from packages.tools.executor import Tool


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """Named operation exposed to the model, backed by a bounded tool."""

    name: str
    version: str
    operation: str
    source_type: EvidenceSourceType
    tool: Tool

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
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                "service_error_rate", "1", "service_error_rate", EvidenceSourceType.METRIC, metrics
            ),
            RegisteredTool(
                "service_latency", "1", "service_latency", EvidenceSourceType.METRIC, metrics
            ),
            RegisteredTool(
                "db_connection_pressure",
                "1",
                "db_connection_pressure",
                EvidenceSourceType.METRIC,
                metrics,
            ),
            RegisteredTool(
                "kafka_consumer_lag", "1", "kafka_consumer_lag", EvidenceSourceType.METRIC, metrics
            ),
            RegisteredTool("service_logs", "1", "query_logs", EvidenceSourceType.LOG, logs),
            RegisteredTool(
                "service_error_logs", "1", "find_log_patterns", EvidenceSourceType.LOG, logs
            ),
            RegisteredTool("slow_traces", "1", "search_traces", EvidenceSourceType.TRACE, traces),
            RegisteredTool("trace_detail", "1", "get_trace", EvidenceSourceType.TRACE, traces),
        )
    )
