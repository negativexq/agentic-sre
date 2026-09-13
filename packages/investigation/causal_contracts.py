"""Versioned A1 structured causal and STOP contracts."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from packages.investigation.contracts import (
    HypothesisMechanism,
    HypothesisSubmission,
    InvestigationModel,
    StopReason,
)
from packages.investigation.topology import DependencyResourceId, WorkloadComponentId


class TriggerType(StrEnum):
    """Stable causal event classes, independent of fixture names."""

    ERROR_RATE_INCREASE = "ERROR_RATE_INCREASE"
    SERVICE_LATENCY_INCREASE = "SERVICE_LATENCY_INCREASE"
    DEPENDENCY_LATENCY_INCREASE = "DEPENDENCY_LATENCY_INCREASE"
    DB_CONNECTION_PRESSURE = "DB_CONNECTION_PRESSURE"
    DB_QUERY_LATENCY_INCREASE = "DB_QUERY_LATENCY_INCREASE"
    CONSUMER_LAG_INCREASE = "CONSUMER_LAG_INCREASE"
    CONSUMER_FAILURE = "CONSUMER_FAILURE"
    POD_RESTART = "POD_RESTART"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"


class EvidenceCategory(StrEnum):
    """Small operational categories usable in bounded STOP metadata."""

    METRICS = "METRICS"
    LOGS = "LOGS"
    TRACES = "TRACES"
    KUBERNETES = "KUBERNETES"
    CHANGE_HISTORY = "CHANGE_HISTORY"
    DATABASE = "DATABASE"
    MESSAGING = "MESSAGING"


class StructuredTrigger(InvestigationModel):
    """Structured causal trigger with optional resource precision."""

    trigger_type: TriggerType
    trigger_component: WorkloadComponentId | None = None
    trigger_resource: DependencyResourceId | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "StructuredTrigger":
        """Require at least one canonical trigger target."""
        if self.trigger_component is None and self.trigger_resource is None:
            raise ValueError("structured trigger requires a component or resource")
        return self


class CausalHypothesis(InvestigationModel):
    """A1 hypothesis separating workload ownership from resource precision."""

    symptom_component: WorkloadComponentId
    causal_component: WorkloadComponentId
    causal_resource: DependencyResourceId | None = None
    mechanism: HypothesisMechanism
    structured_trigger: StructuredTrigger
    causal_summary: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=12)

    def to_legacy_submission(self) -> HypothesisSubmission:
        """Project to the v0.2 contract without changing A1 primary grading."""
        return HypothesisSubmission(
            affected_component=self.causal_component.value,
            mechanism=self.mechanism,
            suspected_trigger=self.causal_summary,
            evidence_ids=list(self.evidence_ids),
        )


class CausalStopDecision(InvestigationModel):
    """Bounded externally useful STOP metadata without private reasoning."""

    stop_reason: StopReason
    considered_components: list[WorkloadComponentId] = Field(default_factory=list, max_length=8)
    considered_resources: list[DependencyResourceId] = Field(default_factory=list, max_length=8)
    missing_evidence_categories: list[EvidenceCategory] = Field(default_factory=list, max_length=8)


__all__ = [
    "CausalHypothesis",
    "CausalStopDecision",
    "EvidenceCategory",
    "StructuredTrigger",
    "TriggerType",
]
