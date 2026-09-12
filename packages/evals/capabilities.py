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
    available: bool


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
            available=registry is None or set(_REQUIRED_TOOLS[scenario.category]).issubset(names),
        )
        for scenario in scenarios
    )
