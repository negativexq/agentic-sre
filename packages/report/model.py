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

REPORT_VERSION = "1.0"


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


class ReportLifecyclePhase(ReportModel):
    name: str
    at: datetime
    offset_seconds: float
    detail: str


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
    lifecycle: tuple[ReportLifecyclePhase, ...] = ()

    evidence_count: int = 0
    model_calls: int = 0
    mode: str = "deterministic"

    # A short list of the alert names that opened the incident.
    alert_names: tuple[str, ...] = Field(default=())
