"""Runtime-owned workload/resource identities and dependency topology."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import model_validator

from packages.investigation.contracts import InvestigationModel


class WorkloadComponentId(StrEnum):
    """Canonical identities for deployed application workloads."""

    ORDER_SERVICE = "order-service"
    PAYMENT_SERVICE = "payment-service"
    ORDER_WORKER = "order-worker"


class DependencyResourceId(StrEnum):
    """Canonical identities for infrastructure resources used by workloads."""

    POSTGRESQL = "postgresql"
    KAFKA = "kafka"
    REDIS = "redis"


class DependencyTargetType(StrEnum):
    """Target domain of a directed dependency edge."""

    WORKLOAD = "workload"
    RESOURCE = "resource"


class DependencyEdge(InvestigationModel):
    """A directed edge whose source workload depends on one target."""

    source: WorkloadComponentId
    target_workload: WorkloadComponentId | None = None
    target_resource: DependencyResourceId | None = None

    @model_validator(mode="after")
    def validate_one_target(self) -> "DependencyEdge":
        """Prevent ambiguous or target-less topology edges."""
        if (self.target_workload is None) == (self.target_resource is None):
            raise ValueError("an edge must target exactly one workload or resource")
        return self

    @property
    def target_type(self) -> DependencyTargetType:
        """Return the typed target domain."""
        return (
            DependencyTargetType.WORKLOAD
            if self.target_workload is not None
            else DependencyTargetType.RESOURCE
        )

    @property
    def target(self) -> WorkloadComponentId | DependencyResourceId:
        """Return the canonical target identity."""
        return self.target_workload or self.target_resource  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class TopologyRegistry:
    """Small immutable topology registry used by runtime and model context."""

    workloads: tuple[WorkloadComponentId, ...]
    resources: tuple[DependencyResourceId, ...]
    edges: tuple[DependencyEdge, ...]

    def __post_init__(self) -> None:
        """Validate deterministic ordering, membership and duplicate edges."""
        if self.workloads != tuple(sorted(self.workloads, key=str)):
            raise ValueError("workloads must be deterministically ordered")
        if self.resources != tuple(sorted(self.resources, key=str)):
            raise ValueError("resources must be deterministically ordered")
        edge_keys = [self._edge_key(edge) for edge in self.edges]
        if edge_keys != sorted(edge_keys) or len(edge_keys) != len(set(edge_keys)):
            raise ValueError("edges must be sorted and unique")
        workload_set = set(self.workloads)
        resource_set = set(self.resources)
        for edge in self.edges:
            if edge.source not in workload_set:
                raise ValueError("edge source is not a registered workload")
            if edge.target_workload is not None and edge.target_workload not in workload_set:
                raise ValueError("edge target is not a registered workload")
            if edge.target_resource is not None and edge.target_resource not in resource_set:
                raise ValueError("edge target is not a registered resource")

    @staticmethod
    def _edge_key(edge: DependencyEdge) -> tuple[str, str, str]:
        """Build a stable edge sort/comparison key."""
        return (edge.source.value, edge.target_type.value, edge.target.value)

    def workload_components(self) -> tuple[WorkloadComponentId, ...]:
        """Return canonical workload identities."""
        return self.workloads

    def dependency_resources(self) -> tuple[DependencyResourceId, ...]:
        """Return canonical dependency-resource identities."""
        return self.resources

    def validate_workload(self, value: str | WorkloadComponentId) -> WorkloadComponentId:
        """Validate one workload identity against the runtime registry."""
        try:
            candidate = WorkloadComponentId(value)
        except ValueError as error:
            raise ValueError(f"unknown workload component: {value}") from error
        if candidate not in self.workloads:
            raise ValueError(f"workload is not registered: {value}")
        return candidate

    def validate_resource(self, value: str | DependencyResourceId) -> DependencyResourceId:
        """Validate one dependency-resource identity against the registry."""
        try:
            candidate = DependencyResourceId(value)
        except ValueError as error:
            raise ValueError(f"unknown dependency resource: {value}") from error
        if candidate not in self.resources:
            raise ValueError(f"resource is not registered: {value}")
        return candidate

    def dependencies_for(self, workload: str | WorkloadComponentId) -> tuple[DependencyEdge, ...]:
        """Return all resources/workloads on which a source workload depends."""
        source = self.validate_workload(workload)
        return tuple(edge for edge in self.edges if edge.source is source)

    def serialize(self) -> dict[str, Any]:
        """Return a bounded, model-safe deterministic topology projection."""
        return {
            "workloads": [item.value for item in self.workloads],
            "resources": [item.value for item in self.resources],
            "edge_semantics": "source workload depends on target",
            "dependencies": [
                {
                    "source": edge.source.value,
                    "target_type": edge.target_type.value,
                    "target": edge.target.value,
                }
                for edge in self.edges
            ],
        }

    def canonical_json(self) -> str:
        """Return stable serialized topology bytes as text."""
        return json.dumps(self.serialize(), sort_keys=True, separators=(",", ":"))

    def topology_hash(self) -> str:
        """Hash the full deterministic topology projection."""
        return sha256(self.canonical_json().encode()).hexdigest()

    def component_registry_hash(self) -> str:
        """Hash the identity registries independently of dependency edges."""
        payload = {
            "workloads": [item.value for item in self.workloads],
            "resources": [item.value for item in self.resources],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


DEFAULT_TOPOLOGY = TopologyRegistry(
    workloads=tuple(sorted(WorkloadComponentId, key=str)),
    resources=tuple(sorted(DependencyResourceId, key=str)),
    edges=tuple(
        sorted(
            (
                DependencyEdge(
                    source=WorkloadComponentId.ORDER_SERVICE,
                    target_workload=WorkloadComponentId.PAYMENT_SERVICE,
                ),
                DependencyEdge(
                    source=WorkloadComponentId.ORDER_SERVICE,
                    target_resource=DependencyResourceId.POSTGRESQL,
                ),
                DependencyEdge(
                    source=WorkloadComponentId.ORDER_SERVICE,
                    target_resource=DependencyResourceId.KAFKA,
                ),
                DependencyEdge(
                    source=WorkloadComponentId.PAYMENT_SERVICE,
                    target_resource=DependencyResourceId.POSTGRESQL,
                ),
                DependencyEdge(
                    source=WorkloadComponentId.ORDER_WORKER,
                    target_resource=DependencyResourceId.KAFKA,
                ),
                DependencyEdge(
                    source=WorkloadComponentId.ORDER_WORKER,
                    target_resource=DependencyResourceId.REDIS,
                ),
            ),
            key=TopologyRegistry._edge_key,
        )
    ),
)


@dataclass(frozen=True, slots=True)
class QueryTarget:
    """Canonical target identity extracted from validated tool arguments."""

    workload: WorkloadComponentId | None = None
    resource: DependencyResourceId | None = None


def target_from_tool_arguments(
    arguments: Mapping[str, Any], *, topology: TopologyRegistry = DEFAULT_TOPOLOGY
) -> QueryTarget:
    """Extract a canonical target without assigning causal ownership."""
    for key in ("service", "consumer", "deployment"):
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        try:
            return QueryTarget(workload=topology.validate_workload(value))
        except ValueError:
            try:
                return QueryTarget(resource=topology.validate_resource(value))
            except ValueError:
                return QueryTarget()
    return QueryTarget()


@dataclass(frozen=True, slots=True)
class QueryTargetUsage:
    """Deterministic aggregate of validated tool target records."""

    workloads: tuple[WorkloadComponentId, ...]
    resources: tuple[DependencyResourceId, ...]
    outside_alert_scope_executions: int


def derive_query_target_usage(
    records: Iterable[Mapping[str, Any]],
    *,
    alert_scope: str | WorkloadComponentId,
    topology: TopologyRegistry = DEFAULT_TOPOLOGY,
) -> QueryTargetUsage:
    """Derive target breadth from execution records, never model prose."""
    scope = topology.validate_workload(alert_scope)
    workloads: set[WorkloadComponentId] = set()
    resources: set[DependencyResourceId] = set()
    outside = 0
    for record in records:
        workload_value = record.get("target_workload")
        resource_value = record.get("target_resource")
        target = QueryTarget(
            workload=(
                topology.validate_workload(workload_value)
                if isinstance(workload_value, str)
                else None
            ),
            resource=(
                topology.validate_resource(resource_value)
                if isinstance(resource_value, str)
                else None
            ),
        )
        if target.workload is not None:
            workloads.add(topology.validate_workload(target.workload))
            if target.workload is not scope:
                outside += 1
        if target.resource is not None:
            resources.add(topology.validate_resource(target.resource))
    return QueryTargetUsage(
        tuple(sorted(workloads, key=str)), tuple(sorted(resources, key=str)), outside
    )


__all__ = [
    "DEFAULT_TOPOLOGY",
    "DependencyEdge",
    "DependencyResourceId",
    "DependencyTargetType",
    "QueryTarget",
    "QueryTargetUsage",
    "TopologyRegistry",
    "WorkloadComponentId",
    "derive_query_target_usage",
    "target_from_tool_arguments",
]
