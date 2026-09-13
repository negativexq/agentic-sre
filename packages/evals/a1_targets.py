"""Evaluator-only structured targets for the A1 compatibility set."""

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.evals.dataset import FROZEN_DATASET
from packages.evals.live_fixtures import FIXTURE_BY_NAME
from packages.investigation.causal_contracts import StructuredTrigger, TriggerType
from packages.investigation.contracts import HypothesisMechanism
from packages.investigation.topology import (
    DEFAULT_TOPOLOGY,
    DependencyResourceId,
    WorkloadComponentId,
)


class A1EvaluationTarget(BaseModel):
    """Private expected outcome and subset metadata for one A1 scenario."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(min_length=1, max_length=64)
    fixture: str = Field(min_length=1, max_length=100)
    symptom_component: WorkloadComponentId
    causal_component: WorkloadComponentId
    causal_resource: DependencyResourceId | None = None
    mechanism: HypothesisMechanism
    structured_trigger: StructuredTrigger
    alert_scope_component: WorkloadComponentId
    cross_component_opportunity: bool
    change_evidence_opportunity: bool

    @model_validator(mode="after")
    def validate_relations(self) -> "A1EvaluationTarget":
        """Keep derived subset metadata consistent with canonical identities."""
        if self.cross_component_opportunity != (
            self.alert_scope_component is not self.causal_component
        ):
            raise ValueError("cross-component opportunity does not match component identities")
        if self.structured_trigger.trigger_component not in {
            None,
            self.symptom_component,
            self.causal_component,
        }:
            raise ValueError("trigger workload must describe the symptom or causal workload")
        if self.structured_trigger.trigger_resource not in {None, self.causal_resource}:
            raise ValueError("trigger resource must match the causal resource when present")
        return self


_COMPATIBILITY_SPECS: dict[str, dict[str, object]] = {
    "V020-001": {
        "symptom_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.ERROR_RATE_INCREASE,
        "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-002": {
        "symptom_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.ERROR_RATE_INCREASE,
        "trigger_component": WorkloadComponentId.ORDER_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-003": {
        "symptom_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.DEPENDENCY_LATENCY_INCREASE,
        "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-004": {
        "symptom_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.SERVICE_LATENCY_INCREASE,
        "trigger_component": WorkloadComponentId.ORDER_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-005": {
        "symptom_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_resource": DependencyResourceId.POSTGRESQL,
        "trigger_type": TriggerType.DB_CONNECTION_PRESSURE,
        "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
        "trigger_resource": DependencyResourceId.POSTGRESQL,
        "change_evidence_opportunity": False,
    },
    "V020-006": {
        "symptom_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_component": WorkloadComponentId.ORDER_SERVICE,
        "causal_resource": DependencyResourceId.POSTGRESQL,
        "trigger_type": TriggerType.DB_QUERY_LATENCY_INCREASE,
        "trigger_component": WorkloadComponentId.ORDER_SERVICE,
        "trigger_resource": DependencyResourceId.POSTGRESQL,
        "change_evidence_opportunity": False,
    },
    "V020-007": {
        "symptom_component": WorkloadComponentId.ORDER_WORKER,
        "causal_component": WorkloadComponentId.ORDER_WORKER,
        "causal_resource": DependencyResourceId.KAFKA,
        "trigger_type": TriggerType.CONSUMER_LAG_INCREASE,
        "trigger_component": WorkloadComponentId.ORDER_WORKER,
        "trigger_resource": DependencyResourceId.KAFKA,
        "change_evidence_opportunity": False,
    },
    "V020-008": {
        "symptom_component": WorkloadComponentId.ORDER_WORKER,
        "causal_component": WorkloadComponentId.ORDER_WORKER,
        "causal_resource": None,
        "trigger_type": TriggerType.CONSUMER_FAILURE,
        "trigger_component": WorkloadComponentId.ORDER_WORKER,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-009": {
        "symptom_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.POD_RESTART,
        "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": False,
    },
    "V020-010": {
        "symptom_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
        "causal_resource": None,
        "trigger_type": TriggerType.CONFIGURATION_CHANGE,
        "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
        "trigger_resource": None,
        "change_evidence_opportunity": True,
    },
}


def _build_target(scenario_id: str, spec: dict[str, object]) -> A1EvaluationTarget:
    """Build one target from the manually audited evaluator specification."""
    scenario = next(item for item in FROZEN_DATASET if item.scenario_id == scenario_id)
    definition = FIXTURE_BY_NAME.get(scenario.fixture)
    if definition is None:
        raise ValueError(f"fixture is not registered: {scenario.fixture}")
    trigger = StructuredTrigger(
        trigger_type=spec["trigger_type"],  # type: ignore[arg-type]
        trigger_component=spec["trigger_component"],  # type: ignore[arg-type]
        trigger_resource=spec["trigger_resource"],  # type: ignore[arg-type]
    )
    return A1EvaluationTarget(
        scenario_id=scenario_id,
        fixture=scenario.fixture,
        symptom_component=spec["symptom_component"],  # type: ignore[arg-type]
        causal_component=spec["causal_component"],  # type: ignore[arg-type]
        causal_resource=spec["causal_resource"],  # type: ignore[arg-type]
        mechanism=scenario.mechanism,
        structured_trigger=trigger,
        alert_scope_component=DEFAULT_TOPOLOGY.validate_workload(definition.service),
        cross_component_opportunity=(definition.service != spec["causal_component"]),
        change_evidence_opportunity=bool(spec["change_evidence_opportunity"]),
    )


def _validate_compatibility_targets() -> tuple[A1EvaluationTarget, ...]:
    """Validate one target per frozen scenario in frozen dataset order."""
    dataset_ids = tuple(item.scenario_id for item in FROZEN_DATASET)
    if set(_COMPATIBILITY_SPECS) != set(dataset_ids):
        raise ValueError("A1 compatibility target IDs do not match the frozen dataset")
    targets = tuple(_build_target(item, _COMPATIBILITY_SPECS[item]) for item in dataset_ids)
    for scenario, target in zip(FROZEN_DATASET, targets, strict=True):
        definition = FIXTURE_BY_NAME[scenario.fixture]
        if (
            target.fixture != scenario.fixture
            or target.alert_scope_component.value != definition.service
        ):
            raise ValueError(f"A1 target metadata mismatch for {scenario.scenario_id}")
        if target.cross_component_opportunity != (
            target.alert_scope_component is not target.causal_component
        ):
            raise ValueError(f"A1 scope relation mismatch for {scenario.scenario_id}")
    return targets


A1_COMPATIBILITY_TARGETS = _validate_compatibility_targets()
A1_TARGET_BY_SCENARIO = {item.scenario_id: item for item in A1_COMPATIBILITY_TARGETS}


def a1_compatibility_target_hash() -> str:
    """Return a stable hash for the evaluator-only target mapping."""
    payload = [item.model_dump(mode="json") for item in A1_COMPATIBILITY_TARGETS]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "A1EvaluationTarget",
    "A1_COMPATIBILITY_TARGETS",
    "A1_TARGET_BY_SCENARIO",
    "a1_compatibility_target_hash",
]
