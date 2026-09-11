"""Strict, JSON-safe Pydantic contracts for the deterministic core."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.enums import (
    ActionExecutionStatus,
    ActionType,
    AlertSource,
    AlertStatus,
    EvidenceSourceType,
    HypothesisStatus,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    PolicyDecision,
    RiskClass,
    VerificationStatus,
)

IncidentId = UUID
AlertId = UUID
IncidentEventId = UUID
EvidenceId = UUID
HypothesisId = UUID
ProposalId = UUID
ActionRequestId = UUID
AuthorizedActionId = UUID
ActionExecutionId = UUID
VerificationCheckId = UUID
VerificationResultId = UUID
ToolCallId = UUID


class ContractModel(BaseModel):
    """Base configuration shared by all public domain contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)


class TimeWindow(ContractModel):
    """Inclusive UTC observation interval."""

    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def validate_order(self) -> "TimeWindow":
        """Reject an interval whose end predates its start."""
        if self.ends_at < self.starts_at:
            raise ValueError("ends_at must be greater than or equal to starts_at")
        return self


class Incident(ContractModel):
    """Current materialized state of an incident."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    incident_id: IncidentId = Field(default_factory=uuid4)
    status: IncidentStatus
    severity: IncidentSeverity
    source: IncidentSource
    title: str = Field(min_length=1)
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    correlation_id: UUID = Field(default_factory=uuid4)


class Alert(ContractModel):
    """Normalized alert attached to an incident."""

    alert_id: AlertId = Field(default_factory=uuid4)
    alert_name: str = Field(min_length=1)
    service: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    fingerprint: str = Field(min_length=1)
    status: AlertStatus
    source: AlertSource


class IncidentEvent(ContractModel):
    """Immutable audit event belonging to one incident."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    event_id: IncidentEventId = Field(default_factory=uuid4)
    incident_id: IncidentId
    event_type: IncidentEventType
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: UUID


class Evidence(ContractModel):
    """Provenance-preserving normalized observation."""

    evidence_id: EvidenceId = Field(default_factory=uuid4)
    incident_id: IncidentId
    source_type: EvidenceSourceType
    source_system: str = Field(min_length=1)
    observation: dict[str, Any]
    time_window: TimeWindow
    tool_call_id: ToolCallId
    raw_result_reference: str = Field(min_length=1)
    collected_at: datetime


class Hypothesis(ContractModel):
    """Structured hypothesis contract without an inference engine."""

    hypothesis_id: HypothesisId = Field(default_factory=uuid4)
    incident_id: IncidentId
    affected_component: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    suspected_trigger: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    counter_evidence_ids: list[EvidenceId] = Field(default_factory=list)
    status: HypothesisStatus


class RemediationProposal(ContractModel):
    """Typed remediation proposal; execution is outside v0.1.0 scope."""

    proposal_id: ProposalId = Field(default_factory=uuid4)
    incident_id: IncidentId
    action_type: ActionType
    target: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    risk_class: RiskClass
    reversible: bool


class PolicyEvaluation(ContractModel):
    """Deterministic policy decision for a proposed action."""

    decision: PolicyDecision
    reason: str = Field(min_length=1)
    evaluated_at: datetime
    policy_version: str = Field(min_length=1)


class ActionRequest(ContractModel):
    """Requested infrastructure action from the typed action vocabulary."""

    request_id: ActionRequestId = Field(default_factory=uuid4)
    incident_id: IncidentId
    action_type: ActionType
    target: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class AuthorizedAction(ContractModel):
    """Authorization record for a request; not an execution command."""

    authorization_id: AuthorizedActionId = Field(default_factory=uuid4)
    request_id: ActionRequestId
    decision: PolicyDecision
    authorized_at: datetime
    authorized_by: str = Field(min_length=1)


class ActionExecution(ContractModel):
    """Audit record reserved for a future execution boundary."""

    execution_id: ActionExecutionId = Field(default_factory=uuid4)
    authorization_id: AuthorizedActionId
    status: ActionExecutionStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class VerificationCheck(ContractModel):
    """One deterministic check in a verification result."""

    check_id: VerificationCheckId = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    passed: bool
    observation: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(ContractModel):
    """Aggregate outcome of verification checks."""

    result_id: VerificationResultId = Field(default_factory=uuid4)
    incident_id: IncidentId
    status: VerificationStatus
    checks: list[VerificationCheck] = Field(default_factory=list)
    observed_at: datetime
    summary: str = Field(min_length=1)
