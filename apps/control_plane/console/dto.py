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


# --- diagnosis -------------------------------------------------------------


class DiagnosisView(ConsoleModel):
    """The engine's answer for one incident, shaped for the workspace."""

    incident_id: str
    resolution: str
    confidence: str
    # The leading actor is the top-ranked actor even when unresolved; it equals
    # the root cause only once the diagnosis is RESOLVED.
    leading_root_actor: str | None
    root_cause: str | None
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
    status: str  # sent | failed
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
