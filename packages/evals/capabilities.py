"""Deterministic capability matrix for the frozen live benchmark."""

from dataclasses import dataclass

from packages.evals.dataset import FROZEN_DATASET, FrozenIncident
from packages.investigation.registry import ReadOnlyToolRegistry


@dataclass(frozen=True, slots=True)
class ScenarioCapability:
    """One frozen scenario's required read-only evidence surface."""

    scenario_id: str
    category: str
    required_tools: tuple[str, ...]
    tool_capability: bool
    argument_readiness: bool
    temporal_readiness: bool
    provenance_readiness: bool

    @property
    def available(self) -> bool:
        """Backward-compatible overall readiness flag."""
        return all(
            (
                self.tool_capability,
                self.argument_readiness,
                self.temporal_readiness,
                self.provenance_readiness,
            )
        )


_REQUIRED_TOOLS: dict[str, tuple[str, ...]] = {
    "service_error": ("service_error_rate", "service_error_logs"),
    "dependency_latency": ("service_latency", "slow_traces"),
    "service_latency": ("service_latency", "slow_traces"),
    "database": ("db_connection_pressure", "db_query_latency"),
    "kafka": ("kafka_consumer_lag", "service_logs"),
    "kubernetes": ("kubernetes_pods", "kubernetes_events", "kubernetes_container_restarts"),
    "deployment": ("recent_deployment_changes", "recent_configuration_changes"),
}


def capability_matrix(
    scenarios: tuple[FrozenIncident, ...] = FROZEN_DATASET,
    registry: ReadOnlyToolRegistry | None = None,
) -> tuple[ScenarioCapability, ...]:
    """Return whether each frozen scenario has sufficient live tools."""
    names = set(registry.names()) if registry is not None else set()
    return tuple(
        ScenarioCapability(
            scenario_id=scenario.scenario_id,
            category=scenario.category,
            required_tools=_REQUIRED_TOOLS[scenario.category],
            tool_capability=registry is None
            or set(_REQUIRED_TOOLS[scenario.category]).issubset(names),
            argument_readiness=registry is None
            or all(
                _minimal_args(registry, tool) is not None
                for tool in _REQUIRED_TOOLS[scenario.category]
            ),
            temporal_readiness=True,
            provenance_readiness=True,
        )
        for scenario in scenarios
    )


def _minimal_args(registry: ReadOnlyToolRegistry, name: str) -> dict[str, object] | None:
    """Validate the smallest production-shaped argument set for one tool."""
    values = {
        "service": "payment-service",
        "consumer": "order-worker",
        "deployment": "payment-service",
        "trace_id": "0" * 32,
    }
    try:
        model = registry.get(name).argument_model
        return registry.validate(
            name,
            {key: values[key] for key, field in model.model_fields.items() if field.is_required()},
        )
    except (KeyError, PermissionError, ValueError):
        return None
