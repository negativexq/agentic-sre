"""The immutable incident report snapshot schema.

A report is a frozen artifact: once built for a diagnosis run it never changes,
even if the incident is re-diagnosed. It carries its own ``report_version`` and
pins the ``diagnosis_run_id`` it was taken from, so an old report always renders
the same way from the same stored document. Every causal field is copied from
the stored diagnosis — the report adds no interpretation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

REPORT_VERSION = "2.1"


class ReportModel(BaseModel):
    """Frozen base for report parts."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ReportHop(ReportModel):
    source: str
    relation: str
    target: str


class ReportFinding(ReportModel):
    kind: str
    entity: str
    at: datetime | None
    summary: str
    temporal_role: str
    onset_delta_seconds: float | None
    evidence_ids: tuple[str, ...] = ()


class ReportAlternative(ReportModel):
    actor: str
    epistemic_state: str | None
    score: float | None
    note: str


class ReportEliminationCheck(ReportModel):
    name: str
    passed: bool
    detail: str


class ReportElimination(ReportModel):
    """Why an alternative was excluded, copied from the rule's audit record."""

    hypothesis_id: str
    actor: str
    reason_code: str
    rule: str
    consequence: str | None
    mechanism: str
    detail: str
    evidence_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    coverage_basis: str = ""
    preconditions: tuple[ReportEliminationCheck, ...] = ()


class ReportLifecyclePhase(ReportModel):
    name: str
    at: datetime
    offset_seconds: float
    detail: str


class ReportInvestigationState(ReportModel):
    hypothesis_id: str
    actor: str
    state: str


class ReportInvestigationGap(ReportModel):
    gap_id: str
    dimension: str
    missing_fact: str
    resolvability: str


class ReportInvestigationTurn(ReportModel):
    turn_index: int
    gap_id: str | None
    gap_dimension: str | None
    missing_fact: str | None
    intent_id: str | None
    intent_kind: str | None
    action: str
    capability: str | None
    target: str | None
    bounded_query: dict[str, object] | None
    action_rationale: str
    authorization_result: str
    authorization_reason: str
    backend_execution_status: str
    observation_id: str | None
    observation_outcome: str | None
    returned_evidence_refs: tuple[str, ...] = ()
    new_evidence_refs: tuple[str, ...] = ()
    already_known_refs: tuple[str, ...] = ()
    normalized_finding_ids: tuple[str, ...] = ()
    affected_hypothesis_ids: tuple[str, ...] = ()
    resolution_before: str
    resolution_after: str | None
    hypothesis_states_before: tuple[ReportInvestigationState, ...] = ()
    hypothesis_states_after: tuple[ReportInvestigationState, ...] = ()
    gap_states_before: tuple[ReportInvestigationGap, ...] = ()
    gap_states_after: tuple[ReportInvestigationGap, ...] = ()
    decision_state_changed: bool | None
    progress_classification: str


class ReportInvestigationSummary(ReportModel):
    initial_resolution: str
    final_resolution: str
    turns: int
    model_calls: int
    tool_calls: int
    unique_observations: int
    unique_evidence_added: int
    stop_reason: str
    initial_hypotheses: tuple[ReportInvestigationState, ...] = ()
    initial_information_gaps: tuple[ReportInvestigationGap, ...] = ()


class ReportAgentSafetyAudit(ReportModel):
    selected_actions: int
    authorized_actions: int
    rejected_actions: int
    executed_reads: int
    out_of_policy_executions: int
    write_executions: int
    secret_accesses: int


class ReportAgentContribution(ReportModel):
    resolution_changed: bool
    root_actor_changed: bool
    hypotheses_changed: bool
    alternatives_eliminated: int
    new_evidence_added: int
    decision_state_changed: bool


class ReportSnapshot(ReportModel):
    """The full immutable report for one diagnosis run of one incident."""

    report_id: str
    report_version: str = REPORT_VERSION
    incident_id: str
    diagnosis_run_id: str | None
    generated_at: datetime

    title: str
    severity: str
    status: str

    incident_started_at: datetime | None
    diagnosed_at: datetime | None
    diagnosis_duration_seconds: float | None
    affected_services: tuple[str, ...] = ()

    root_actor: str | None
    leading_root_actor: str | None
    confidence: str
    resolution: str
    is_resolved: bool
    summary: str
    resolution_rationale: str | None

    causal_path: tuple[ReportHop, ...] = ()
    initiating_findings: tuple[ReportFinding, ...] = ()
    supporting_findings: tuple[ReportFinding, ...] = ()
    contradictory_findings: tuple[ReportFinding, ...] = ()
    evidence: tuple[ReportFinding, ...] = ()
    alternatives: tuple[ReportAlternative, ...] = ()
    eliminations: tuple[ReportElimination, ...] = ()
    lifecycle: tuple[ReportLifecyclePhase, ...] = ()

    evidence_count: int = 0
    model_calls: int = 0
    mode: str = "deterministic"

    # A short list of the alert names that opened the incident.
    alert_names: tuple[str, ...] = Field(default=())

    investigation_summary: ReportInvestigationSummary | None = None
    investigation_timeline: tuple[ReportInvestigationTurn, ...] = ()
    decision_relevant_observations: tuple[ReportInvestigationTurn, ...] = ()
    non_contributing_observations: tuple[ReportInvestigationTurn, ...] = ()
    remaining_information_gaps: tuple[ReportInvestigationGap, ...] = ()
    agent_safety_audit: ReportAgentSafetyAudit | None = None
    agent_contribution: ReportAgentContribution | None = None
