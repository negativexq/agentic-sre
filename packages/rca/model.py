"""Typed data shared by observation sources, signal extraction, and diagnosis."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CLUSTER_SCOPE = "_cluster"


class EntityRef(BaseModel):
    """A Kubernetes object identity rendered as ``namespace/Kind/name``."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    namespace: str = CLUSTER_SCOPE

    @property
    def canonical(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"

    @classmethod
    def parse(cls, value: str) -> EntityRef:
        parts = value.split("/", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"expected namespace/Kind/name, got {value!r}")
        return cls(namespace=parts[0], kind=parts[1], name=parts[2])

    def __str__(self) -> str:
        return self.canonical


class Alert(BaseModel):
    """One firing alert occurrence."""

    model_config = ConfigDict(frozen=True)

    name: str
    service: str | None = None
    namespace: str | None = None
    starts_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class Lifecycle(StrEnum):
    """How an object version came to be recorded.

    OBSERVED: first sighting of an object that existed before observation began.
    CREATED: the object appeared while it was being observed.
    UPDATED: its desired state changed.
    DELETED: tombstone; ``body`` is the last state seen before deletion.
    """

    OBSERVED = "OBSERVED"
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"


class JournalEntry(BaseModel):
    """One stored object version, as the storage layer hands it to the engine."""

    model_config = ConfigDict(frozen=True)

    object_key: str
    observed_at: datetime
    body: dict[str, Any]
    version_id: int
    lifecycle: Lifecycle


class ObjectVersion(BaseModel):
    """One observed version of a Kubernetes object."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    observed_at: datetime
    body: dict[str, Any]
    evidence_id: str
    lifecycle: Lifecycle = Lifecycle.UPDATED


class LogRecord(BaseModel):
    """One warning or error log line from a service."""

    model_config = ConfigDict(frozen=True)

    service: str
    at: datetime | None
    severity: str
    message: str
    evidence_id: str


class ResourcePressure(BaseModel):
    """Use of one container resource relative to its limit, before and after a time.

    memory: peak working set / limit. cpu: throttled / scheduled CFS periods.
    ``baseline`` is None when no samples precede the split time.
    """

    model_config = ConfigDict(frozen=True)

    pod: EntityRef
    container: str
    resource: str
    baseline: float | None
    peak: float
    at: datetime | None
    evidence_id: str


class TrafficObservation(BaseModel):
    """One bounded request/traffic measurement used for change detection."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    metric: str
    at: datetime
    value: float
    evidence_id: str


class ClusterEvent(BaseModel):
    """One Kubernetes event about an involved object."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    reason: str
    type: str = "Normal"
    message: str = ""
    first_at: datetime | None = None
    last_at: datetime | None = None
    count: int = 1
    evidence_id: str


class Edge(BaseModel):
    """A directed structural relation between two objects."""

    model_config = ConfigDict(frozen=True)

    source: EntityRef
    target: EntityRef
    relation: str


class CausalHop(BaseModel):
    """One cause-to-symptom hop with an honest rendered relation."""

    model_config = ConfigDict(frozen=True)

    source: EntityRef
    relation: str
    target: EntityRef
    direction: str = "forward"


class EvidenceTemporalRole(StrEnum):
    """The deterministic role an observation can play around symptom onset."""

    INITIATING = "INITIATING"
    SUPPORTING = "SUPPORTING"
    CONSEQUENCE = "CONSEQUENCE"
    AMBIGUOUS = "AMBIGUOUS"


class PredicateStatus(StrEnum):
    """Outcome of one deterministic verification predicate."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    WEAK = "WEAK"


class VerificationPredicate(BaseModel):
    """An inspectable, non-LLM verification decision."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: PredicateStatus
    evidence_ids: tuple[str, ...] = ()
    detail: str = ""


class VerificationTrace(BaseModel):
    """The evidence and predicates behind the final confidence label."""

    model_config = ConfigDict(frozen=True)

    candidate: EntityRef
    decision: Confidence | None = None
    predicates: tuple[VerificationPredicate, ...] = ()
    onset_delta_seconds: float | None = None
    supporting_evidence: tuple[str, ...] = ()
    contradictory_evidence: tuple[str, ...] = ()
    score_margin: float | None = None
    rationale: str = ""


class FindingKind(StrEnum):
    """Deterministic signal categories, ordered roughly by causal strength."""

    CONFIG_CHANGE = "CONFIG_CHANGE"
    OBJECT_CREATED = "OBJECT_CREATED"
    OBJECT_DELETED = "OBJECT_DELETED"
    SPEC_CHANGE = "SPEC_CHANGE"
    IMAGE_CHANGE = "IMAGE_CHANGE"
    SCALE_CHANGE = "SCALE_CHANGE"
    ROLLOUT_RESTART = "ROLLOUT_RESTART"
    FAULT_INJECTION = "FAULT_INJECTION"
    FAULT_SCHEDULE = "FAULT_SCHEDULE"
    POLICY_CREATED = "POLICY_CREATED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    NETWORK_RESTRICTION = "NETWORK_RESTRICTION"
    CONTAINER_FAILURE = "CONTAINER_FAILURE"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    DEPENDENCY_ERRORS = "DEPENDENCY_ERRORS"
    FAILURE_EVENT = "FAILURE_EVENT"
    AUTOSCALING_FAILURE = "AUTOSCALING_FAILURE"
    TRAFFIC_INCREASE = "TRAFFIC_INCREASE"


class Finding(BaseModel):
    """One deterministic observation attached to an entity."""

    model_config = ConfigDict(frozen=True)

    kind: FindingKind
    entity: EntityRef
    at: datetime | None
    summary: str
    evidence_ids: tuple[str, ...] = ()
    related: tuple[EntityRef, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)
    temporal_role: EvidenceTemporalRole = EvidenceTemporalRole.AMBIGUOUS
    incident_onset: datetime | None = None
    onset_delta_seconds: float | None = None


class Symptoms(BaseModel):
    """What the incident looks like from its alerts."""

    model_config = ConfigDict(frozen=True)

    onset: datetime | None
    last_seen: datetime | None
    services: tuple[str, ...]
    namespaces: tuple[str, ...]
    alert_names: tuple[str, ...]
    background_alert_counts: dict[str, int] = Field(default_factory=dict)


class Candidate(BaseModel):
    """A ranked root-cause candidate with the reasons for its score."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    score: float
    findings: tuple[Finding, ...]
    linked_symptoms: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    causal_path: tuple[CausalHop, ...] = ()
    causal_explanation: str = "UNLINKED"


class Confidence(StrEnum):
    """How well the chosen root cause is supported.

    VERIFIED: a deterministic rule ties the cause to the symptoms.
    LIKELY: strong signal and a structural link, but no verifying rule.
    UNVERIFIED: best available guess.
    """

    VERIFIED = "VERIFIED"
    LIKELY = "LIKELY"
    UNVERIFIED = "UNVERIFIED"


class Resolution(StrEnum):
    """Whether deterministic evidence distinguishes the leading hypotheses."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class HypothesisSignature(BaseModel):
    """Name-independent structure used to compare causal hypotheses."""

    model_config = ConfigDict(frozen=True)

    initiating_kinds: tuple[str, ...] = ()
    supporting_kinds: tuple[str, ...] = ()
    contradiction_kinds: tuple[str, ...] = ()
    temporal_profile: tuple[str, ...] = ()
    symptom_relation: str = "UNLINKED"
    causal_path_shape: tuple[tuple[tuple[str, str, str], ...], ...] = ()
    manifestation_shape: tuple[str, ...] = ()
    provenance_classes: tuple[str, ...] = ()


class ResolutionTrace(BaseModel):
    """Deterministic explanation of cross-hypothesis distinguishability."""

    model_config = ConfigDict(frozen=True)

    state: Resolution
    leading_hypothesis_ids: tuple[str, ...] = ()
    signatures: tuple[HypothesisSignature, ...] = ()
    distinguishing_facts: tuple[str, ...] = ()
    unresolved_dimensions: tuple[str, ...] = ()
    eliminated_hypotheses: tuple[str, ...] = ()
    elimination_reasons: tuple[str, ...] = ()
    rationale: str = ""


class Hypothesis(BaseModel):
    """One causal episode assembled from one or more entity candidates.

    Kubernetes object identity is deliberately kept separate from hypothesis
    identity.  ``causal_actor`` is the proposed intervention point while
    ``manifestations`` retain the entities where the episode became visible.
    """

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str
    causal_actor: EntityRef
    members: tuple[EntityRef, ...] = ()
    manifestations: tuple[EntityRef, ...] = ()
    findings: tuple[Finding, ...] = ()
    initiating_findings: tuple[Finding, ...] = ()
    supporting_findings: tuple[Finding, ...] = ()
    contradictory_findings: tuple[Finding, ...] = ()
    causal_paths: tuple[tuple[CausalHop, ...], ...] = ()
    linked_symptoms: tuple[str, ...] = ()
    causal_explanation: str = "UNLINKED"
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    signature: HypothesisSignature | None = None


class HypothesisDiagnostics(BaseModel):
    """Non-authoritative measurements of candidate-to-hypothesis grouping."""

    model_config = ConfigDict(frozen=True)

    raw_candidate_count: int = 0
    hypothesis_count: int = 0
    multi_entity_hypotheses: int = 0
    ownership_chains_collapsed: int = 0
    duplicate_evidence_ids_removed: int = 0
    exact_score_ties: int = 0
    structurally_similar_top_hypotheses: int = 0


class Remediation(BaseModel):
    """A proposed, never executed, corrective action."""

    model_config = ConfigDict(frozen=True)

    action: str
    command: str
    risk: str
    requires_approval: bool = True


class InvestigationStep(BaseModel):
    """One step of the investigation trace."""

    model_config = ConfigDict(frozen=True)

    actor: str
    action: str
    detail: str


class Diagnosis(BaseModel):
    """The product's answer for one incident."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    root_cause: EntityRef | None
    confidence: Confidence
    resolution: Resolution = Resolution.INSUFFICIENT_EVIDENCE
    summary: str
    symptoms: Symptoms
    evidence: tuple[Finding, ...] = ()
    causal_path: tuple[CausalHop, ...] = ()
    causal_explanation: str = "UNLINKED"
    alternatives: tuple[Candidate, ...] = ()
    hypothesis: Hypothesis | None = None
    alternative_hypotheses: tuple[Hypothesis, ...] = ()
    hypothesis_diagnostics: HypothesisDiagnostics | None = None
    remediation: tuple[Remediation, ...] = ()
    steps: tuple[InvestigationStep, ...] = ()
    mode: str = "deterministic"
    model_calls: int = 0
    verification: VerificationTrace | None = None
    resolution_trace: ResolutionTrace | None = None
    ambiguous_hypotheses: tuple[Hypothesis, ...] = ()


__all__ = [
    "CLUSTER_SCOPE",
    "Alert",
    "Candidate",
    "CausalHop",
    "EvidenceTemporalRole",
    "ClusterEvent",
    "Confidence",
    "Resolution",
    "Diagnosis",
    "Edge",
    "EntityRef",
    "Finding",
    "FindingKind",
    "InvestigationStep",
    "Hypothesis",
    "HypothesisDiagnostics",
    "HypothesisSignature",
    "JournalEntry",
    "Lifecycle",
    "LogRecord",
    "ObjectVersion",
    "Remediation",
    "ResourcePressure",
    "TrafficObservation",
    "Symptoms",
    "PredicateStatus",
    "VerificationPredicate",
    "VerificationTrace",
    "ResolutionTrace",
]
