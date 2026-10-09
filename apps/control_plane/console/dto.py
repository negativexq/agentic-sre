"""UI-facing data contracts for the operator console.

These DTOs are the stable surface the frontend renders. They deliberately do
not expose internal persistence rows or the full RCA model — the console reads
these shapes only, so the engine and storage can evolve without breaking the
UI. Nothing here computes a causal claim; every causal field is copied verbatim
from a stored diagnosis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ConsoleModel(BaseModel):
    """Base for console DTOs; forbids unexpected fields on the wire."""

    model_config = ConfigDict(extra="forbid")


# --- shared value shapes ---------------------------------------------------


class CausalHopView(ConsoleModel):
    """One rendered cause-to-symptom hop."""

    source: str
    relation: str
    target: str
    direction: str


class FindingView(ConsoleModel):
    """One deterministic finding attached to an entity."""

    kind: str
    entity: str
    at: datetime | None
    summary: str
    temporal_role: str
    onset_delta_seconds: float | None
    evidence_ids: list[str]


class HypothesisView(ConsoleModel):
    """A competing causal hypothesis with its epistemic state."""

    hypothesis_id: str
    actor: str
    epistemic_state: str
    score: float
    causal_explanation: str
    reasons: list[str]


class CandidateView(ConsoleModel):
    """A ranked alternative actor and its strongest observed signal."""

    entity: str
    score: float
    strongest_signal: str


class RemediationView(ConsoleModel):
    """A proposed, never executed corrective action."""

    action: str
    command: str
    risk: str
    requires_approval: bool


class StepView(ConsoleModel):
    """One row of the investigation trace."""

    actor: str
    action: str
    detail: str


class InvestigationActionAuditView(ConsoleModel):
    """One selected action and its deterministic authorization/execution trace."""

    turn_index: int
    gap_id: str | None
    gap_dimension: str | None
    missing_fact: str | None
    intent_id: str | None
    intent_kind: str | None
    action: str
    capability: str | None
    target: str | None
    action_rationale: str
    authorization_result: str
    authorization_reason: str
    backend_execution_status: str
    observation_id: str | None
    observation_outcome: str | None
    returned_evidence_refs: list[str]
    new_evidence_refs: list[str]
    already_known_refs: list[str]
    normalized_finding_ids: list[str]
    affected_hypothesis_ids: list[str]
    resolution_before: str
    resolution_after: str | None
    decision_state_changed: bool | None
    progress_classification: str


class InvestigationAuditView(ConsoleModel):
    """The persisted investigation artifact bound to a diagnosis run."""

    diagnosis_run_id: str
    artifact_version: str
    initial_resolution: str
    final_resolution: str
    stop_reason: str
    turns: int
    model_calls: int
    tool_calls: int
    action_audits: list[InvestigationActionAuditView]


# --- diagnosis -------------------------------------------------------------


class ExecutingInstanceView(ConsoleModel):
    """What ran for a shown cause (m21 §17; D3): a detail of the cause, never a second cause."""

    actor: str
    instance: str
    instance_uid: str | None = None
    target: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    rule_id: str


class TimingWithheldView(ConsoleModel):
    actor: str
    authority: str
    relations: tuple[str, ...] = ()


class TimingDriverView(ConsoleModel):
    onset: datetime
    diagnosis_status: str
    actor: str
    change: str
    reason: str


class TimingView(ConsoleModel):
    """Timing stability of the decision (M21 timing contract §4, §5); UNASSESSED is shown as not assessed."""

    status: str
    withheld: tuple[TimingWithheldView, ...] = ()
    drivers: tuple[TimingDriverView, ...] = ()


class DiagnosisView(ConsoleModel):
    """The engine's answer for one incident, shaped for the workspace."""

    incident_id: str
    resolution: str
    decision_semantics: str = "legacy"
    diagnosis_status: str = "UNASSESSED"
    claim_level: str = "UNASSESSED"
    incident_recovery: str = "NOT_ASSESSED"
    context_hypothesis_ids: tuple[str, ...] = ()
    material_frontier_ids: tuple[str, ...] = ()
    causal_explanations: tuple[dict[str, Any], ...] = ()
    frontier_answers: tuple[dict[str, Any], ...] = ()
    mechanism_verified_hypothesis_ids: tuple[str, ...] = ()
    confidence: str
    # The leading actor is the top-ranked actor even when unresolved; it equals
    # the root cause only once the diagnosis is RESOLVED. It is None, with the reason
    # below, when the engine did not establish it (roadmap C10).
    leading_root_actor: str | None
    root_cause: str | None
    leading_actor_withheld_reason: str | None = None
    # What the operator is shown (roadmap C12): SINGLE shows leading_root_actor; COMPETING and
    # NOT_ESTABLISHED show the candidates instead, never one of them chosen by name.
    leading_actor_display: str = "SINGLE"
    leading_actor_tier: str | None = None
    leading_actor_candidates: tuple[str, ...] = ()
    # D3 (docs/ui/product-contract.md): read verbatim from the diagnosis, absent rather than guessed.
    executing_instances: tuple[ExecutingInstanceView, ...] = ()
    leader_instance_resolution: str | None = None
    leader_instance: str | None = None
    leader_instance_uid: str | None = None
    timing: TimingView | None = None
    is_resolved: bool
    summary: str
    resolution_rationale: str | None
    unresolved_dimensions: list[str]
    causal_path: list[CausalHopView]
    causal_explanation: str
    initiating_findings: list[FindingView]
    supporting_findings: list[FindingView]
    contradictory_findings: list[FindingView]
    evidence: list[FindingView]
    competing_hypotheses: list[HypothesisView]
    alternatives: list[CandidateView]
    remediation: list[RemediationView]
    steps: list[StepView]
    investigation_audit: InvestigationAuditView | None = None
    services: list[str]
    alert_names: list[str]
    onset: datetime | None
    background_alerts_ignored: int
    model_calls: int
    mode: str


# --- timeline --------------------------------------------------------------


class LifecyclePhaseView(ConsoleModel):
    """One bound phase of the diagnosis run, with its offset from run start."""

    name: str
    at: datetime
    detail: str
    offset_seconds: float


class TimelineEventView(ConsoleModel):
    """One immutable incident-timeline event."""

    event_type: str
    timestamp: datetime


class TimelineView(ConsoleModel):
    """Alert-to-diagnosis lifecycle for one incident, bound to a run."""

    run_id: str | None
    phases: list[LifecyclePhaseView]
    note: str | None
    alert_fired: datetime | None
    incident_opened: datetime | None
    diagnosis_ready: datetime | None
    alert_to_diagnosis_seconds: float | None
    reads: int
    evidence_count: int
    model_calls: int
    events: list[TimelineEventView]


# --- evidence --------------------------------------------------------------


class EvidenceView(ConsoleModel):
    """One provenance-backed raw observation."""

    evidence_id: str
    source_type: str
    source_system: str
    collected_at: datetime
    starts_at: datetime
    ends_at: datetime
    raw_result_reference: str
    observation: dict[str, Any]


# --- incidents -------------------------------------------------------------


class IncidentListItem(ConsoleModel):
    """One incident as it appears in the dashboard and incident list."""

    incident_id: str
    title: str
    status: str
    severity: str
    source: str
    service: str | None
    leading_root_actor: str | None
    confidence: str | None
    leading_actor_withheld_reason: str | None = None
    leading_actor_display: str = "SINGLE"
    leading_actor_candidates: tuple[str, ...] = ()
    resolution: str | None
    has_diagnosis: bool
    created_at: datetime
    updated_at: datetime
    age_seconds: float


class IncidentPage(ConsoleModel):
    """A filtered, paginated slice of incidents."""

    items: list[IncidentListItem]
    total: int
    limit: int
    offset: int


class IncidentDetail(ConsoleModel):
    """Everything the incident workspace needs in one round trip."""

    incident: IncidentListItem
    diagnosis: DiagnosisView | None
    timeline: TimelineView
    evidence_count: int


# --- changes ---------------------------------------------------------------


class ChangeView(ConsoleModel):
    """One recorded change fact, optionally relative to an incident onset."""

    change_id: str
    timestamp: datetime
    resource_type: str
    resource_name: str
    namespace: str | None = None
    change_type: str
    scope: str
    revision: str | None
    source: str | None
    onset_delta_seconds: float | None
    # True when this change touches the actor the engine named as leading — a
    # labeling aid, not a causal claim by the UI (see product-contract.md, G6).
    matches_leading_actor: bool = False


# --- reports ---------------------------------------------------------------


class ReportSummary(ConsoleModel):
    """One immutable report as it appears in the reports library."""

    report_id: str
    incident_id: str
    title: str
    severity: str
    confidence: str
    resolution: str
    root_actor: str | None
    leading_root_actor: str | None
    diagnosis_run_id: str | None
    report_version: str
    generated_at: datetime


# --- email sharing ---------------------------------------------------------


class ShareRequest(ConsoleModel):
    """A request to share a report by email."""

    recipients: list[str]
    include_pdf: bool = True
    idempotency_key: str | None = None


class DeliveryView(ConsoleModel):
    """An audit record of one report share."""

    delivery_id: str
    report_id: str
    recipients: list[str]
    subject: str
    status: str  # pending | sent | failed
    error: str | None
    created_at: datetime


# --- system ----------------------------------------------------------------


class SystemConnector(ConsoleModel):
    """Health of one upstream connector."""

    name: str
    status: str  # connected | degraded | unavailable | not_configured
    detail: str | None


class SystemStatus(ConsoleModel):
    """Connector health for the console."""

    connectors: list[SystemConnector]


# --- settings --------------------------------------------------------------


class SettingsView(ConsoleModel):
    """Read-only effective configuration; secrets are reduced to booleans."""

    watched_namespaces: list[str]
    evidence_namespaces: list[str]
    auto_diagnose: bool
    watch_interval_seconds: float
    cluster_access: str
    llm_enabled: bool
    llm_model: str | None
    llm_max_calls: int
    api_token_configured: bool
    email_configured: bool
    email_sender: str | None
    prometheus_configured: bool
    loki_configured: bool
    tempo_configured: bool
    report_version: str


# --- dashboard -------------------------------------------------------------


class DashboardCounters(ConsoleModel):
    """Top-of-dashboard operational counters."""

    active_incidents: int
    critical_incidents: int
    diagnosing: int
    resolved_diagnoses: int
    median_diagnosis_seconds: float | None


class DashboardSummary(ConsoleModel):
    """The overview screen in one payload."""

    counters: DashboardCounters
    active_incidents: list[IncidentListItem]
    recent_diagnoses: list[IncidentListItem]
    system: SystemStatus
    generated_at: datetime
