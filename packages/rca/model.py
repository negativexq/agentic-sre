"""Typed data shared by observation sources, signal extraction, and diagnosis."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class TraceSpanStatus(StrEnum):
    """Provider-neutral status of one observed trace span."""

    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class TraceSpanObservation(BaseModel):
    """One typed distributed-trace span observed from a read-only telemetry source."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None
    service: str = Field(min_length=1)
    span_name: str | None = None
    span_kind: str | None = None
    start_at: datetime
    end_at: datetime | None = None
    duration_raw: float | None = None
    status: TraceSpanStatus = TraceSpanStatus.UNKNOWN
    semantic_attributes: dict[str, str] = Field(default_factory=dict)
    evidence_id: str = Field(min_length=1)


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
    # Structural plausibility is deliberately separate from observed evidence.
    # It lets a bounded initial view expose actors for later investigation
    # without assigning them artificial score or causal support.
    structural_basis: tuple[str, ...] = ()


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


class HypothesisEpistemicState(StrEnum):
    """The deterministic evidence state of one causal hypothesis."""

    SUPPORTED = "SUPPORTED"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTED = "CONTRADICTED"


class InvestigationStatus(StrEnum):
    """Whether bounded investigation has material work beyond RCA resolution."""

    OPEN = "OPEN"
    EXHAUSTED = "EXHAUSTED"
    NOT_REQUIRED = "NOT_REQUIRED"


class FrontierStatus(StrEnum):
    """Lifecycle of a structurally plausible, not-yet-causal actor."""

    UNEXPLORED = "UNEXPLORED"
    QUERIED_NO_CAUSAL_FINDING = "QUERIED_NO_CAUSAL_FINDING"
    PROMOTED = "PROMOTED"


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


class ResolutionReasonCode(StrEnum):
    """Stable reason codes used when a hypothesis is excluded from resolution."""

    NO_CAUSAL_SYMPTOM_LINK = "NO_CAUSAL_SYMPTOM_LINK"
    NO_ONSET_CAPABLE_INITIATING_EVIDENCE = "NO_ONSET_CAPABLE_INITIATING_EVIDENCE"
    EXPLICIT_TEMPORAL_CONTRADICTION = "EXPLICIT_TEMPORAL_CONTRADICTION"
    STRUCTURALLY_DOMINATED = "STRUCTURALLY_DOMINATED"
    ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT = "ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT"


class ResolutionElimination(BaseModel):
    """A mechanically inspectable reason a hypothesis was not plausible."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str
    code: ResolutionReasonCode
    evidence_ids: tuple[str, ...] = ()
    detail: str = ""


class ResolutionDiscriminator(BaseModel):
    """A fact that distinguishes one leading hypothesis from another."""

    model_config = ConfigDict(frozen=True)

    kind: str
    hypothesis_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    finding_kinds: tuple[str, ...] = ()
    temporal_relations: tuple[str, ...] = ()
    causal_path_shapes: tuple[tuple[tuple[str, str, str], ...], ...] = ()
    detail: str = ""


class DominanceRelation(BaseModel):
    """A structural, evidence-backed dominance relation."""

    model_config = ConfigDict(frozen=True)

    stronger_hypothesis_id: str
    weaker_hypothesis_id: str
    evidence_ids: tuple[str, ...] = ()
    detail: str = ""


class HypothesisResolutionAudit(BaseModel):
    """Bounded per-hypothesis audit data retained in a resolution trace."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str
    signature: HypothesisSignature
    epistemic_state: HypothesisEpistemicState = HypothesisEpistemicState.SUPPORTED
    plausible: bool
    plausibility_reasons: tuple[ResolutionReasonCode, ...] = ()
    verification: VerificationTrace | None = None
    contradictory_evidence_ids: tuple[str, ...] = ()
    onset_relation: tuple[str, ...] = ()
    causal_linkage: str = "UNLINKED"


class ResolutionTrace(BaseModel):
    """Deterministic explanation of cross-hypothesis distinguishability."""

    model_config = ConfigDict(frozen=True)

    state: Resolution
    leading_hypothesis_ids: tuple[str, ...] = ()
    signatures: tuple[HypothesisSignature, ...] = ()
    distinguishing_facts: tuple[str, ...] = ()
    unresolved_dimensions: tuple[str, ...] = ()
    eliminated_hypotheses: tuple[str, ...] = ()
    unresolved_hypotheses: tuple[str, ...] = ()
    elimination_reasons: tuple[str, ...] = ()
    considered_hypotheses: tuple[str, ...] = ()
    plausible_hypotheses: tuple[str, ...] = ()
    eliminations: tuple[ResolutionElimination, ...] = ()
    discriminators: tuple[ResolutionDiscriminator, ...] = ()
    dominance_relations: tuple[DominanceRelation, ...] = ()
    hypothesis_audits: tuple[HypothesisResolutionAudit, ...] = ()
    decision_basis: str = ""
    rationale: str = ""


class GapDimension(StrEnum):
    """Typed evidence dimensions that can distinguish causal hypotheses."""

    CHANGE_TIMING = "CHANGE_TIMING"
    ENTITY_STATE = "ENTITY_STATE"
    METRIC_BASELINE = "METRIC_BASELINE"
    METRIC_CHANGE = "METRIC_CHANGE"
    DEPENDENCY_HEALTH = "DEPENDENCY_HEALTH"
    EVENT_SEQUENCE = "EVENT_SEQUENCE"
    LOG_ERROR_PATTERN = "LOG_ERROR_PATTERN"
    TOPOLOGY_RELATION = "TOPOLOGY_RELATION"
    CONFIG_DIFFERENCE = "CONFIG_DIFFERENCE"
    AUTOSCALING_TARGET_STATE = "AUTOSCALING_TARGET_STATE"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    FAILURE_ONSET = "FAILURE_ONSET"


class GapResolvability(StrEnum):
    """Whether a currently available evidence capability could answer a gap."""

    RESOLVABLE = "RESOLVABLE"
    UNRESOLVABLE_WITH_CURRENT_TOOLS = "UNRESOLVABLE_WITH_CURRENT_TOOLS"
    ALREADY_OBSERVED = "ALREADY_OBSERVED"


class InformationGapOrigin(StrEnum):
    """Whether a gap is causal reasoning work or incident-scope discovery."""

    CAUSAL = "CAUSAL"
    DISCOVERY = "DISCOVERY"


class GapOutcomeKind(StrEnum):
    """Possible deterministic outcomes of an information request."""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NO_DATA = "NO_DATA"
    UNKNOWN = "UNKNOWN"


class InvestigationQuery(BaseModel):
    """Provider-neutral, bounded semantic query parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime | None = None
    end: datetime | None = None
    reasons: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    limit: int = Field(default=32, ge=1, le=64)


class AuthorizedQuery(BaseModel):
    """One exact capability/target pair allowed for an information gap."""

    model_config = ConfigDict(frozen=True)

    capability: str
    target: EntityRef
    alternative_ids: tuple[str, ...] = ()


class GapOutcome(BaseModel):
    """A typed outcome and its implication for a hypothesis set."""

    model_config = ConfigDict(frozen=True)

    kind: GapOutcomeKind
    hypothesis_ids: tuple[str, ...] = ()
    alternative_ids: tuple[str, ...] = ()
    condition: str = ""
    implication: str = ""


class ToolCapability(BaseModel):
    """Metadata for a bounded, read-only evidence capability."""

    model_config = ConfigDict(frozen=True)

    name: str
    dimensions: tuple[GapDimension, ...] = ()
    required_inputs: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()


class InformationGap(BaseModel):
    """A deterministic fact missing between plausible hypotheses."""

    model_config = ConfigDict(frozen=True)

    gap_id: str
    origin: InformationGapOrigin = InformationGapOrigin.CAUSAL
    dimension: GapDimension
    hypothesis_ids: tuple[str, ...] = ()
    alternative_ids: tuple[str, ...] = ()
    entity_scope: tuple[EntityRef, ...] = ()
    known_facts: tuple[str, ...] = ()
    missing_fact: str
    required_relation: str | None = None
    discriminating_outcomes: tuple[GapOutcome, ...] = ()
    authorized_queries: tuple[AuthorizedQuery, ...] = ()
    candidate_tools: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    priority: int = 1
    resolvability: GapResolvability = GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
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
    structural_basis: tuple[str, ...] = ()
    signature: HypothesisSignature | None = None


class StructuralAlternative(BaseModel):
    """A causally plausible actor available for targeted observation.

    This is deliberately not a ``Hypothesis``: it has no causal evidence or
    score.  It becomes a hypothesis only when a deterministic normalizer
    promotes an observed Finding through the ordinary RCA pipeline.
    """

    model_config = ConfigDict(frozen=True)

    alternative_id: str
    actor: EntityRef
    role: str
    structural_basis: tuple[str, ...] = ()
    causal_path: tuple[CausalHop, ...] = ()
    linked_symptoms: tuple[str, ...] = ()
    queryable_dimensions: tuple[GapDimension, ...] = ()
    observation_targets: tuple[EntityRef, ...] = ()
    queried_dimensions: tuple[GapDimension, ...] = ()
    status: FrontierStatus = FrontierStatus.UNEXPLORED


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


class InvestigationAction(BaseModel):
    """Strict model-selected evidence-acquisition action.

    The action deliberately has no root-cause or confidence field.  It is a
    request to inspect one already-authorized information gap only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["inspect", "stop"]
    gap_id: str | None = None
    capability: str | None = None
    target: EntityRef | None = None
    query: InvestigationQuery | None = None
    rationale: str = Field(default="", max_length=400)


class InvestigationStopReason(StrEnum):
    """Why the bounded investigation graph terminated."""

    RESOLVED = "RESOLVED"
    NO_RESOLVABLE_GAP = "NO_RESOLVABLE_GAP"
    NO_PROGRESS = "NO_PROGRESS"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"
    WALL_TIME_EXHAUSTED = "WALL_TIME_EXHAUSTED"
    POLICY_STOP = "POLICY_STOP"
    MODEL_FAILURE = "MODEL_FAILURE"
    TOOL_ERROR = "TOOL_ERROR"


class InvestigationActionStatus(StrEnum):
    """Graph routing result after deterministic action validation."""

    VALID_INSPECT = "VALID_INSPECT"
    VALID_STOP = "VALID_STOP"
    INVALID_RETRY = "INVALID_RETRY"
    INVALID_EXHAUSTED = "INVALID_EXHAUSTED"


class InvestigationObservation(BaseModel):
    """Bounded typed output from one read-only semantic investigation tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    gap_id: str
    capability: str
    target: EntityRef
    observed_at: datetime | None = None
    outcome: GapOutcomeKind = GapOutcomeKind.UNKNOWN
    hypothesis_ids: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    source_class: str = "investigation"
    error: str | None = None


class InvestigationLedgerEntry(BaseModel):
    """Append-only accounting for one semantic investigation query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str
    gap_id: str
    capability: str
    target: EntityRef
    query: InvestigationQuery | None = None
    returned_evidence_refs: tuple[str, ...] = ()
    new_evidence_refs: tuple[str, ...] = ()
    already_known_refs: tuple[str, ...] = ()
    normalized_finding_ids: tuple[str, ...] = ()
    affected_hypothesis_ids: tuple[str, ...] = ()
    outcome: GapOutcomeKind = GapOutcomeKind.UNKNOWN


class InvestigationExecutionStatus(StrEnum):
    """Whether the selected action reached a backend and what happened there."""

    NOT_EXECUTED = "NOT_EXECUTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class InvestigationHypothesisState(BaseModel):
    """One hypothesis state captured at an investigation decision boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str
    actor: EntityRef
    state: HypothesisEpistemicState


class InvestigationGapState(BaseModel):
    """One currently visible information gap and its deterministic availability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    resolvability: GapResolvability


class InvestigationActionAudit(BaseModel):
    """Structured lifecycle and decision accounting for one selected action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_index: int
    action: InvestigationAction
    intent_id: str | None = None
    intent_kind: str | None = None
    gap_dimension: GapDimension | None = None
    missing_fact: str | None = None
    authorization_result: Literal["AUTHORIZED", "REJECTED", "POLICY_STOP"] = "REJECTED"
    authorization_reason: str = ""
    backend_execution_status: InvestigationExecutionStatus = (
        InvestigationExecutionStatus.NOT_EXECUTED
    )
    observation_id: str | None = None
    observation_outcome: GapOutcomeKind | None = None
    returned_evidence_refs: tuple[str, ...] = ()
    new_evidence_refs: tuple[str, ...] = ()
    already_known_refs: tuple[str, ...] = ()
    normalized_finding_ids: tuple[str, ...] = ()
    affected_hypothesis_ids: tuple[str, ...] = ()
    resolution_before: Resolution
    resolution_after: Resolution | None = None
    leading_actor_before: EntityRef | None = None
    leading_actor_after: EntityRef | None = None
    hypothesis_states_before: tuple[InvestigationHypothesisState, ...] = ()
    hypothesis_states_after: tuple[InvestigationHypothesisState, ...] = ()
    gap_states_before: tuple[InvestigationGapState, ...] = ()
    gap_states_after: tuple[InvestigationGapState, ...] = ()
    decision_state_changed: bool | None = None
    progress_classification: str = "PENDING"


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
    information_gaps: tuple[InformationGap, ...] = ()
    structural_alternatives: tuple[StructuralAlternative, ...] = ()
    investigation_status: InvestigationStatus = InvestigationStatus.NOT_REQUIRED

    @model_validator(mode="before")
    @classmethod
    def _backfill_resolution_for_legacy_documents(cls, value: Any) -> Any:
        """Keep pre-resolution stored diagnoses meaningful when reloaded."""
        if isinstance(value, dict) and "resolution" not in value:
            value = dict(value)
            value["resolution"] = (
                Resolution.RESOLVED.value
                if value.get("root_cause") is not None
                else Resolution.INSUFFICIENT_EVIDENCE.value
            )
        return value


class InvestigationResult(BaseModel):
    """Serializable result of one bounded evidence-acquisition run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diagnosis: Diagnosis
    initial_diagnosis: Diagnosis | None = None
    initial_resolution: Resolution
    final_resolution: Resolution
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    unique_observations: int = 0
    unique_evidence_added: int = 0
    attempted_gap_ids: tuple[str, ...] = ()
    rejected_actions: int = 0
    no_data_observations: int = 0
    stop_reason: InvestigationStopReason
    observations: tuple[InvestigationObservation, ...] = ()
    ledger: tuple[InvestigationLedgerEntry, ...] = ()
    action_audits: tuple[InvestigationActionAudit, ...] = ()
    new_evidence_refs: tuple[str, ...] = ()
    resolved_during_investigation: bool = False


__all__ = [
    "CLUSTER_SCOPE",
    "Alert",
    "Candidate",
    "CausalHop",
    "EvidenceTemporalRole",
    "ClusterEvent",
    "Confidence",
    "Resolution",
    "HypothesisEpistemicState",
    "InvestigationStatus",
    "FrontierStatus",
    "Diagnosis",
    "Edge",
    "EntityRef",
    "Finding",
    "FindingKind",
    "InvestigationStep",
    "InvestigationAction",
    "InvestigationLedgerEntry",
    "InvestigationQuery",
    "AuthorizedQuery",
    "InvestigationActionStatus",
    "InvestigationStopReason",
    "InvestigationObservation",
    "InvestigationResult",
    "Hypothesis",
    "StructuralAlternative",
    "HypothesisDiagnostics",
    "HypothesisSignature",
    "ResolutionReasonCode",
    "ResolutionElimination",
    "ResolutionDiscriminator",
    "DominanceRelation",
    "HypothesisResolutionAudit",
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
    "GapDimension",
    "GapResolvability",
    "InformationGapOrigin",
    "GapOutcomeKind",
    "GapOutcome",
    "ToolCapability",
    "InformationGap",
]
