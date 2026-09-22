"""Deterministically assemble a report snapshot from a stored diagnosis."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from packages.rca.model import Candidate, CausalHop, Diagnosis, Finding, Hypothesis, ResolutionTrace
from packages.rca.report import LifecyclePhase
from packages.report.model import (
    REPORT_VERSION,
    ReportAlternative,
    ReportFinding,
    ReportHop,
    ReportLifecyclePhase,
    ReportSnapshot,
)


def _finding(finding: Finding) -> ReportFinding:
    return ReportFinding(
        kind=finding.kind.value,
        entity=finding.entity.canonical,
        at=finding.at,
        summary=finding.summary,
        temporal_role=finding.temporal_role.value,
        onset_delta_seconds=finding.onset_delta_seconds,
        evidence_ids=tuple(finding.evidence_ids),
    )


def _hop(hop: CausalHop) -> ReportHop:
    return ReportHop(
        source=hop.source.canonical, relation=hop.relation, target=hop.target.canonical
    )


def _epistemic_states(trace: ResolutionTrace | None) -> dict[str, str]:
    if trace is None:
        return {}
    return {audit.hypothesis_id: audit.epistemic_state.value for audit in trace.hypothesis_audits}


def _hypothesis_alt(hypothesis: Hypothesis, states: dict[str, str]) -> ReportAlternative:
    return ReportAlternative(
        actor=hypothesis.causal_actor.canonical,
        epistemic_state=states.get(hypothesis.hypothesis_id, "UNRESOLVED"),
        score=hypothesis.score,
        note=hypothesis.reasons[0] if hypothesis.reasons else "",
    )


def _candidate_alt(candidate: Candidate) -> ReportAlternative:
    note = (
        candidate.findings[0].summary
        if candidate.findings
        else "structurally plausible actor; evidence not yet observed"
    )
    return ReportAlternative(
        actor=candidate.entity.canonical, epistemic_state=None, score=candidate.score, note=note
    )


def _seconds(earlier: datetime | None, later: datetime | None) -> float | None:
    if earlier is None or later is None:
        return None
    delta = (later - earlier).total_seconds()
    return delta if delta >= 0 else None


def build_report(
    *,
    incident_id: str,
    title: str,
    severity: str,
    status: str,
    diagnosis: Diagnosis,
    run_id: str | None,
    alert_fired: datetime | None,
    incident_opened: datetime | None,
    diagnosed_at: datetime | None,
    phases: tuple[LifecyclePhase, ...],
    evidence_count: int,
    generated_at: datetime,
) -> ReportSnapshot:
    """Freeze a report from a stored diagnosis and its recorded timing.

    The snapshot is self-contained; the caller persists it verbatim. Re-running
    with the same inputs yields the same content apart from ``report_id`` and
    ``generated_at``, which identify this particular freezing.
    """
    is_resolved = diagnosis.resolution.value == "RESOLVED"
    leading = diagnosis.root_cause.canonical if diagnosis.root_cause else None
    states = _epistemic_states(diagnosis.resolution_trace)
    hypotheses = diagnosis.alternative_hypotheses or diagnosis.ambiguous_hypotheses
    alternatives = (
        tuple(_hypothesis_alt(item, states) for item in hypotheses)
        if hypotheses
        else tuple(_candidate_alt(item) for item in diagnosis.alternatives)
    )
    hypothesis = diagnosis.hypothesis
    started = diagnosis.symptoms.onset or alert_fired or incident_opened
    origin = phases[0].at if phases else None
    return ReportSnapshot(
        report_id=str(uuid4()),
        report_version=REPORT_VERSION,
        incident_id=incident_id,
        diagnosis_run_id=run_id,
        generated_at=generated_at,
        title=title,
        severity=severity,
        status=status,
        incident_started_at=started,
        diagnosed_at=diagnosed_at,
        diagnosis_duration_seconds=_seconds(started, diagnosed_at),
        affected_services=tuple(diagnosis.symptoms.services),
        root_actor=leading if is_resolved else None,
        leading_root_actor=leading,
        confidence=diagnosis.confidence.value,
        resolution=diagnosis.resolution.value,
        is_resolved=is_resolved,
        summary=diagnosis.summary,
        resolution_rationale=(
            diagnosis.resolution_trace.rationale if diagnosis.resolution_trace else None
        ),
        causal_path=tuple(_hop(hop) for hop in diagnosis.causal_path),
        initiating_findings=tuple(
            _finding(f) for f in (hypothesis.initiating_findings if hypothesis else ())
        ),
        supporting_findings=tuple(
            _finding(f) for f in (hypothesis.supporting_findings if hypothesis else ())
        ),
        contradictory_findings=tuple(
            _finding(f) for f in (hypothesis.contradictory_findings if hypothesis else ())
        ),
        evidence=tuple(_finding(f) for f in diagnosis.evidence),
        alternatives=alternatives,
        lifecycle=tuple(
            ReportLifecyclePhase(
                name=phase.name,
                at=phase.at,
                offset_seconds=_seconds(origin, phase.at) or 0.0,
                detail=phase.detail,
            )
            for phase in phases
        ),
        evidence_count=evidence_count,
        model_calls=diagnosis.model_calls,
        mode=diagnosis.mode,
        alert_names=tuple(diagnosis.symptoms.alert_names),
    )
