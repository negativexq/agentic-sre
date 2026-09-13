"""Allow-listed read-only investigation tool registry."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import EvidenceSourceType
from packages.investigation.contracts import ToolRepeatPolicy
from packages.investigation.tool_contracts import (
    AnyToolArguments,
    ConsumerArgs,
    DeploymentArgs,
    ServiceArgs,
    ServicePatternArgs,
    ServiceWindowArgs,
    ToolArguments,
    ToolArgumentValidationError,
    TraceIdArgs,
    descriptor_schema,
)
from packages.investigation.topology import DEFAULT_TOPOLOGY, TopologyRegistry
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
    argument_model: type[ToolArguments] = AnyToolArguments
    historical_change_source: bool = False
    repeat_policy: ToolRepeatPolicy = ToolRepeatPolicy.FIXED_WINDOW
    target_argument: str | None = None
    target_topology: TopologyRegistry | None = None

    def descriptor(self) -> dict[str, Any]:
        """Return the safe model-facing capability descriptor."""
        arguments = descriptor_schema(self.argument_model)
        if self.target_argument is not None and self.target_topology is not None:
            field = arguments.get(self.target_argument)
            if field is not None:
                field["enum"] = [item.value for item in self.target_topology.workload_components()]
        return {
            "name": self.name,
            "version": self.version,
            "purpose": self.purpose or f"Read-only {self.operation} observation",
            "evidence_type": self.source_type.value,
            "repeat_policy": self.repeat_policy.value,
            "arguments": arguments,
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and canonicalize arguments before a backend can execute."""
        try:
            parsed = self.argument_model.model_validate(arguments)
        except ValidationError as error:
            first = error.errors()[0]
            location = ".".join(str(item) for item in first.get("loc", ()))
            raise ToolArgumentValidationError(
                f"$.{location}" if location else "$",
                "tool arguments do not match the registered contract",
            ) from error
        canonical = parsed.model_dump(mode="json", exclude_none=True)
        if self.target_argument is not None and self.target_topology is not None:
            value = canonical.get(self.target_argument)
            if not isinstance(value, str):
                raise ToolArgumentValidationError(
                    f"$.{self.target_argument}", "target must be a registered workload"
                )
            try:
                canonical[self.target_argument] = self.target_topology.validate_workload(
                    value
                ).value
            except ValueError as error:
                raise ToolArgumentValidationError(
                    f"$.{self.target_argument}", "target must be a registered workload"
                ) from error
        return canonical

    def request(
        self,
        incident_id: UUID,
        arguments: dict[str, Any],
        *,
        observation_window: dict[str, str] | None = None,
        temporal_mode: str = "INCIDENT_WINDOW",
    ) -> ToolRequest:
        """Build a bounded invocation while preventing operation override."""
        canonical = self.validate_arguments(arguments)
        parameters = {
            **canonical,
            "operation": self.operation,
            "temporal_mode": temporal_mode,
        }
        if observation_window is not None:
            parameters["observation_window"] = observation_window
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

    def has_historical_change_source(self) -> bool:
        """Report whether both change tools read an actual historical journal."""
        return all(
            self._tools[name].historical_change_source
            for name in ("recent_deployment_changes", "recent_configuration_changes")
            if name in self._tools
        ) and all(
            name in self._tools
            for name in ("recent_deployment_changes", "recent_configuration_changes")
        )

    def validate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate one request without executing it."""
        return self.get(name).validate_arguments(arguments)


def live_observability_registry(
    prometheus_url: str,
    loki_url: str,
    tempo_url: str,
    *,
    change_reader: Any | None = None,
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
    change_backend = KubernetesChangeBackend(kubernetes, change_reader)
    changes = change_read_tool(change_backend.query)
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                "service_error_rate",
                "1",
                "service_error_rate",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded HTTP error rate for an explicitly named service.",
                ServiceArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "service_latency",
                "1",
                "service_latency",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded HTTP latency for an explicitly named service.",
                ServiceArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "db_connection_pressure",
                "1",
                "db_connection_pressure",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded database connection-acquisition pressure for an explicitly named service.",
                ServiceArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kafka_consumer_lag",
                "1",
                "kafka_consumer_lag",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded Kafka consumer lag for an explicitly named workload consumer.",
                ConsumerArgs,
                target_argument="consumer",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "db_query_latency",
                "1",
                "db_query_latency",
                EvidenceSourceType.METRIC,
                metrics,
                "Measure bounded database query latency for an explicitly named service.",
                ServiceArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "service_logs",
                "1",
                "query_logs",
                EvidenceSourceType.LOG,
                logs,
                "Search bounded structured logs for an explicitly named service.",
                ServiceWindowArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "service_error_logs",
                "1",
                "find_log_patterns",
                EvidenceSourceType.LOG,
                logs,
                "Find bounded error patterns in an explicitly named service's logs.",
                ServicePatternArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "slow_traces",
                "1",
                "search_traces",
                EvidenceSourceType.TRACE,
                traces,
                "Search bounded slow traces for an explicitly named service.",
                ServiceArgs,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "trace_detail",
                "1",
                "get_trace",
                EvidenceSourceType.TRACE,
                traces,
                "Retrieve one bounded trace by its trace identifier.",
                TraceIdArgs,
            ),
            RegisteredTool(
                "kubernetes_pods",
                "1",
                "get_pods",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded pod health and restart state for a named deployment.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.CURRENT_STATE,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kubernetes_deployment",
                "1",
                "get_deployment",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded deployment replica and image state.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.CURRENT_STATE,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kubernetes_events",
                "1",
                "get_events",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded Kubernetes events for a named deployment.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.FIXED_WINDOW,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kubernetes_rollout_history",
                "1",
                "get_rollout_history",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded deployment revision metadata.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.FIXED_WINDOW,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kubernetes_container_restarts",
                "1",
                "get_container_restarts",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded container restart counts for a named deployment.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.CURRENT_STATE,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "kubernetes_resource_state",
                "1",
                "get_resource_state",
                EvidenceSourceType.KUBERNETES,
                k8s_tool,
                "Inspect bounded current deployment resource state.",
                DeploymentArgs,
                repeat_policy=ToolRepeatPolicy.CURRENT_STATE,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "recent_deployment_changes",
                "1",
                "recent_deployment_changes",
                EvidenceSourceType.CHANGE,
                changes,
                "Inspect bounded historical deployment revision and image changes for a named workload.",
                DeploymentArgs,
                change_backend.has_historical_source,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
            RegisteredTool(
                "recent_configuration_changes",
                "1",
                "recent_configuration_changes",
                EvidenceSourceType.CHANGE,
                changes,
                "Inspect bounded historical configuration changes for a named workload deployment.",
                DeploymentArgs,
                change_backend.has_historical_source,
                target_argument="deployment",
                target_topology=DEFAULT_TOPOLOGY,
            ),
        )
    )
