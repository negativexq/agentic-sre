"""Evaluator-only A1 generalization scenarios and frozen target metadata."""

import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.a1_targets import A1EvaluationTarget
from packages.evals.live_fixtures import GENERALIZATION_FIXTURE_BY_NAME
from packages.investigation.causal_contracts import StructuredTrigger, TriggerType
from packages.investigation.contracts import HypothesisMechanism
from packages.investigation.topology import (
    DEFAULT_TOPOLOGY,
    DependencyResourceId,
    WorkloadComponentId,
)


class ObservableEvidenceSurface(BaseModel):
    """Evaluator-only read-only surface required to qualify a scenario."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class A1GeneralizationScenario(BaseModel):
    """One unseen scenario's fixture and evaluator-side target."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(pattern=r"^A1G-[0-9]{3}$")
    category: str = Field(min_length=1, max_length=100)
    fixture: str = Field(min_length=1, max_length=100)
    alert_name: str = Field(min_length=1, max_length=100)
    alert_scope_component: WorkloadComponentId
    target: A1EvaluationTarget
    observable_evidence_surfaces: list[ObservableEvidenceSurface] = Field(
        min_length=1, max_length=8
    )


def _scenario(
    scenario_id: str,
    category: str,
    fixture: str,
    *,
    symptom: WorkloadComponentId,
    causal: WorkloadComponentId,
    resource: DependencyResourceId | None,
    mechanism: HypothesisMechanism,
    trigger_type: TriggerType,
    trigger_component: WorkloadComponentId,
    trigger_resource: DependencyResourceId | None,
    surfaces: list[ObservableEvidenceSurface],
    change_opportunity: bool = False,
) -> A1GeneralizationScenario:
    """Build one target while validating it against the production fixture registry."""
    definition = GENERALIZATION_FIXTURE_BY_NAME.get(fixture)
    if definition is None:
        raise ValueError(f"generalization fixture is not registered: {fixture}")
    scope = DEFAULT_TOPOLOGY.validate_workload(definition.service)
    if scope is not symptom:
        raise ValueError(f"fixture scope does not match symptom for {scenario_id}")
    target = A1EvaluationTarget(
        scenario_id=scenario_id,
        fixture=fixture,
        symptom_component=symptom,
        causal_component=causal,
        causal_resource=resource,
        mechanism=mechanism,
        structured_trigger=StructuredTrigger(
            trigger_type=trigger_type,
            trigger_component=trigger_component,
            trigger_resource=trigger_resource,
        ),
        alert_scope_component=scope,
        cross_component_opportunity=scope is not causal,
        change_evidence_opportunity=change_opportunity,
    )
    if definition.alert_name != GENERALIZATION_FIXTURE_BY_NAME[fixture].alert_name:
        raise ValueError(f"generalization alert mapping mismatch for {scenario_id}")
    return A1GeneralizationScenario(
        scenario_id=scenario_id,
        category=category,
        fixture=fixture,
        alert_name=definition.alert_name,
        alert_scope_component=scope,
        target=target,
        observable_evidence_surfaces=surfaces,
    )


A1_GENERALIZATION_SCENARIOS: tuple[A1GeneralizationScenario, ...] = (
    _scenario(
        "A1G-001",
        "cross_service_dependency_failure",
        "a1g_order_payment_failure",
        symptom=WorkloadComponentId.ORDER_SERVICE,
        causal=WorkloadComponentId.PAYMENT_SERVICE,
        resource=None,
        mechanism=HypothesisMechanism.DEPENDENCY_FAILURE,
        trigger_type=TriggerType.DEPENDENCY_FAILURE,
        trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
        trigger_resource=None,
        surfaces=[
            ObservableEvidenceSurface(
                tool="service_error_rate", arguments={"service": "order-service"}
            ),
            ObservableEvidenceSurface(
                tool="service_error_rate", arguments={"service": "payment-service"}
            ),
        ],
    ),
    _scenario(
        "A1G-002",
        "shared_dependency_database_pressure",
        "a1g_order_payment_db_pressure",
        symptom=WorkloadComponentId.ORDER_SERVICE,
        causal=WorkloadComponentId.PAYMENT_SERVICE,
        resource=DependencyResourceId.POSTGRESQL,
        mechanism=HypothesisMechanism.DATABASE_CONNECTION_PRESSURE,
        trigger_type=TriggerType.DB_CONNECTION_PRESSURE,
        trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
        trigger_resource=DependencyResourceId.POSTGRESQL,
        surfaces=[
            ObservableEvidenceSurface(
                tool="service_latency", arguments={"service": "order-service"}
            ),
            ObservableEvidenceSurface(
                tool="db_connection_pressure", arguments={"service": "payment-service"}
            ),
            ObservableEvidenceSurface(
                tool="service_latency", arguments={"service": "payment-service"}
            ),
        ],
    ),
    _scenario(
        "A1G-003",
        "same_component_negative_control",
        "a1g_order_config_latency",
        symptom=WorkloadComponentId.ORDER_SERVICE,
        causal=WorkloadComponentId.ORDER_SERVICE,
        resource=None,
        mechanism=HypothesisMechanism.CONFIGURATION_REGRESSION,
        trigger_type=TriggerType.CONFIGURATION_CHANGE,
        trigger_component=WorkloadComponentId.ORDER_SERVICE,
        trigger_resource=None,
        surfaces=[
            ObservableEvidenceSurface(
                tool="service_latency", arguments={"service": "order-service"}
            ),
            ObservableEvidenceSurface(
                tool="recent_configuration_changes", arguments={"deployment": "order-service"}
            ),
        ],
        change_opportunity=True,
    ),
    _scenario(
        "A1G-004",
        "same_component_database_resource",
        "a1g_payment_db_query_latency",
        symptom=WorkloadComponentId.PAYMENT_SERVICE,
        causal=WorkloadComponentId.PAYMENT_SERVICE,
        resource=DependencyResourceId.POSTGRESQL,
        mechanism=HypothesisMechanism.DATABASE_QUERY_LATENCY,
        trigger_type=TriggerType.DB_QUERY_LATENCY_INCREASE,
        trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
        trigger_resource=DependencyResourceId.POSTGRESQL,
        surfaces=[
            ObservableEvidenceSurface(
                tool="service_latency", arguments={"service": "payment-service"}
            ),
            ObservableEvidenceSurface(
                tool="db_query_latency", arguments={"service": "payment-service"}
            ),
        ],
    ),
)


def _canonical(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def generalization_target_hash() -> str:
    """Hash only evaluator target fields, excluding qualification results."""
    payload = [item.target.model_dump(mode="json") for item in A1_GENERALIZATION_SCENARIOS]
    return sha256(_canonical(payload).encode()).hexdigest()


def generalization_dataset_hash() -> str:
    """Hash scenario identity, fixture, alert and qualification surfaces."""
    payload = [
        {
            "scenario_id": item.scenario_id,
            "category": item.category,
            "fixture": item.fixture,
            "alert_name": item.alert_name,
            "alert_scope_component": item.alert_scope_component.value,
            "observable_evidence_surfaces": [
                surface.model_dump(mode="json") for surface in item.observable_evidence_surfaces
            ],
        }
        for item in A1_GENERALIZATION_SCENARIOS
    ]
    return sha256(_canonical(payload).encode()).hexdigest()


__all__ = [
    "A1GeneralizationScenario",
    "A1_GENERALIZATION_SCENARIOS",
    "ObservableEvidenceSurface",
    "generalization_dataset_hash",
    "generalization_target_hash",
]
