"""Offline tests for the runtime-owned A1 topology registry."""

import pytest

from packages.investigation import (
    DEFAULT_TOPOLOGY,
    DependencyResourceId,
    DependencyTargetType,
    WorkloadComponentId,
    derive_query_target_usage,
    target_from_tool_arguments,
)


def test_topology_is_deterministic_and_hashable() -> None:
    assert DEFAULT_TOPOLOGY.serialize() == {
        "workloads": ["order-service", "order-worker", "payment-service"],
        "resources": ["kafka", "postgresql", "redis"],
        "dependencies": [
            {"source": "order-service", "target_type": "resource", "target": "kafka"},
            {"source": "order-service", "target_type": "resource", "target": "postgresql"},
            {"source": "order-service", "target_type": "workload", "target": "payment-service"},
            {"source": "order-worker", "target_type": "resource", "target": "kafka"},
            {"source": "order-worker", "target_type": "resource", "target": "redis"},
            {"source": "payment-service", "target_type": "resource", "target": "postgresql"},
        ],
    }
    assert DEFAULT_TOPOLOGY.topology_hash() == DEFAULT_TOPOLOGY.topology_hash()
    assert DEFAULT_TOPOLOGY.component_registry_hash() == DEFAULT_TOPOLOGY.component_registry_hash()


def test_topology_direction_means_source_depends_on_target() -> None:
    dependencies = DEFAULT_TOPOLOGY.dependencies_for(WorkloadComponentId.ORDER_SERVICE)
    assert any(
        edge.target_workload is WorkloadComponentId.PAYMENT_SERVICE
        and edge.target_type is DependencyTargetType.WORKLOAD
        for edge in dependencies
    )
    assert any(
        edge.target_resource is DependencyResourceId.POSTGRESQL
        and edge.target_type is DependencyTargetType.RESOURCE
        for edge in dependencies
    )


def test_workload_and_resource_domains_are_separate() -> None:
    assert (
        DEFAULT_TOPOLOGY.validate_workload("payment-service") is WorkloadComponentId.PAYMENT_SERVICE
    )
    assert DEFAULT_TOPOLOGY.validate_resource("postgresql") is DependencyResourceId.POSTGRESQL
    with pytest.raises(ValueError):
        DEFAULT_TOPOLOGY.validate_workload("postgresql")
    with pytest.raises(ValueError):
        DEFAULT_TOPOLOGY.validate_resource("payment-service")


def test_tool_target_extraction_and_usage_are_not_causal_claims() -> None:
    target = target_from_tool_arguments({"service": "payment-service"})
    assert target.workload is WorkloadComponentId.PAYMENT_SERVICE
    assert target.resource is None

    usage = derive_query_target_usage(
        [
            {"target_workload": "order-service"},
            {"target_workload": "payment-service"},
            {"target_resource": "postgresql"},
        ],
        alert_scope=WorkloadComponentId.ORDER_SERVICE,
    )
    assert usage.workloads == (
        WorkloadComponentId.ORDER_SERVICE,
        WorkloadComponentId.PAYMENT_SERVICE,
    )
    assert usage.resources == (DependencyResourceId.POSTGRESQL,)
    assert usage.outside_alert_scope_executions == 1


def test_unknown_target_is_not_promoted_to_canonical_identity() -> None:
    target = target_from_tool_arguments({"service": "not-registered"})
    assert target.workload is None
    assert target.resource is None
