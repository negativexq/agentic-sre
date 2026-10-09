"""Pure functions mapping domain objects onto console DTOs.

Every causal field is copied straight from the stored diagnosis; the mappers
add no interpretation. Timing is derived only from recorded timestamps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.control_plane.console.dto import (
    CandidateView,
    CausalHopView,
    ChangeView,
    DiagnosisView,
    EvidenceView,
    ExecutingInstanceView,
    FindingView,
    HypothesisView,
    IncidentListItem,
    InvestigationActionAuditView,
    InvestigationAuditView,
    LifecyclePhaseView,
    RemediationView,
    ReportSummary,
    StepView,
    TimelineEventView,
    TimelineView,
    TimingDriverView,
    TimingView,
    TimingWithheldView,
)
from apps.control_plane.timeline import diagnosis_phases, newer_run_note
from packages.contracts import (
    Alert,
    ChangeRecord,
    Evidence,
    Incident,
    IncidentEvent,
)
from packages.rca.model import (
    Candidate,
    CausalHop,
    Diagnosis,
    EntityRef,
    ExecutingInstance,
    Finding,
    Hypothesis,
    InvestigationResult,
    ResolutionTrace,
    TimingAssessment,
)
from packages.rca.presentation import leader_instance, shown_executing_instances
from packages.report import ReportSnapshot


def _finding_view(finding: Finding) -> FindingView:
    return FindingView(
        kind=finding.kind.value,
        entity=finding.entity.canonical,
        at=finding.at,
        summary=finding.summary,
        temporal_role=finding.temporal_role.value,
        onset_delta_seconds=finding.onset_delta_seconds,
        evidence_ids=list(finding.evidence_ids),
    )


def _hop_view(hop: CausalHop) -> CausalHopView:
    return CausalHopView(
        source=hop.source.canonical,
        relation=hop.relation,
        target=hop.target.canonical,
        direction=hop.direction,
    )


def _epistemic_states(trace: ResolutionTrace | None) -> dict[str, str]:
    if trace is None:
        return {}
    return {audit.hypothesis_id: audit.epistemic_state.value for audit in trace.hypothesis_audits}


def _hypothesis_view(hypothesis: Hypothesis, states: dict[str, str]) -> HypothesisView:
    return HypothesisView(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=hypothesis.causal_actor.canonical,
        epistemic_state=states.get(hypothesis.hypothesis_id, "UNRESOLVED"),
        score=hypothesis.score,
        causal_explanation=hypothesis.causal_explanation,
        reasons=list(hypothesis.reasons),
    )


def _candidate_view(candidate: Candidate) -> CandidateView:
    signal = (
        candidate.findings[0].summary
        if candidate.findings
        else "structurally plausible actor; evidence not yet observed"
    )
    return CandidateView(
        entity=candidate.entity.canonical,
        score=candidate.score,
        strongest_signal=signal,
    )


def _executing_view(item: ExecutingInstance) -> ExecutingInstanceView:
    return ExecutingInstanceView(
        actor=item.actor.canonical,
        instance=item.instance.canonical,
        instance_uid=item.instance_uid,
        target=item.target.canonical if item.target else None,
        started_at=item.started_at,
        ended_at=item.ended_at,
        rule_id=item.rule_id,
    )


def _timing_view(timing: TimingAssessment) -> TimingView:
    return TimingView(
        status=timing.status.value,
        withheld=tuple(
            TimingWithheldView(actor=w.actor, authority=w.authority, relations=w.relations)
            for w in timing.withheld
        ),
        drivers=tuple(
            TimingDriverView(
                onset=outcome.onset,
                diagnosis_status=outcome.diagnosis_status,
                actor=driver.actor,
                change=driver.change,
                reason=driver.reason,
            )
            for outcome in timing.outcomes
            for driver in outcome.drivers
        ),
    )


def presented_leader(
    root_cause: str | None,
    display: str | None,
    candidates: tuple[str, ...],
    withheld_reason: str | None,
) -> tuple[str, tuple[str, ...], str | None]:
    """(display, candidates, the single actor to show or None) for any stored diagnosis.

    A document from before the projection (no candidates) keeps its old presentation: its root cause,
    unless the earlier C10 flag withheld it.
    """
    if not candidates:
        if withheld_reason:
            return "NOT_ESTABLISHED", ((root_cause,) if root_cause else ()), None
        return "SINGLE", ((root_cause,) if root_cause else ()), root_cause
    display = display or "SINGLE"
    return display, candidates, candidates[0] if display == "SINGLE" else None


def diagnosis_view(
    diagnosis: Diagnosis,
    *,
    investigation: InvestigationResult | None = None,
    diagnosis_run_id: str | None = None,
    artifact_version: str | None = None,
) -> DiagnosisView:
    """Shape a stored diagnosis for the workspace, preserving its semantics."""
    is_resolved = diagnosis.resolution.value == "RESOLVED"
    display, candidates, leading = presented_leader(
        diagnosis.root_cause.canonical if diagnosis.root_cause else None,
        diagnosis.leading_actor_display,
        tuple(ref.canonical for ref in diagnosis.leading_actor_candidates),
        diagnosis.leading_actor_withheld_reason,
    )
    states = _epistemic_states(diagnosis.resolution_trace)

    hypotheses = diagnosis.alternative_hypotheses or diagnosis.ambiguous_hypotheses
    contexts = (
        set(diagnosis.resolution_trace.context_hypotheses) if diagnosis.resolution_trace else set()
    )
    competing = [
        _hypothesis_view(item, states).model_copy(update={"epistemic_state": "OBSERVED_CONTEXT"})
        if item.hypothesis_id in contexts
        else _hypothesis_view(item, states)
        for item in hypotheses
    ]

    hypothesis = diagnosis.hypothesis
    initiating = hypothesis.initiating_findings if hypothesis else ()
    supporting = hypothesis.supporting_findings if hypothesis else ()
    contradictory = hypothesis.contradictory_findings if hypothesis else ()

    trace = diagnosis.resolution_trace
    symptoms = diagnosis.symptoms
    instance = leader_instance(diagnosis)
    return DiagnosisView(
        incident_id=diagnosis.incident_id,
        resolution=diagnosis.resolution.value,
        decision_semantics=diagnosis.decision_semantics,
        diagnosis_status=diagnosis.resolution_trace.diagnosis_status
        if diagnosis.resolution_trace
        else "UNASSESSED",
        claim_level=diagnosis.resolution_trace.claim_level
        if diagnosis.resolution_trace
        else "UNASSESSED",
        incident_recovery=diagnosis.incident_recovery,
        context_hypothesis_ids=diagnosis.resolution_trace.context_hypotheses
        if diagnosis.resolution_trace
        else (),
        material_frontier_ids=diagnosis.resolution_trace.material_frontier_ids
        if diagnosis.resolution_trace
        else (),
        causal_explanations=tuple(r.model_dump(mode="json") for r in trace.explanations)
        if trace
        else (),
        frontier_answers=tuple(a.model_dump(mode="json") for a in trace.frontier_answers)
        if trace
        else (),
        mechanism_verified_hypothesis_ids=trace.mechanism_verified_hypotheses if trace else (),
        confidence=diagnosis.confidence.value,
        leading_root_actor=leading,
        root_cause=leading if is_resolved else None,
        leading_actor_withheld_reason=diagnosis.leading_actor_withheld_reason,
        leading_actor_display=display,
        leading_actor_tier=diagnosis.leading_actor_tier,
        leading_actor_candidates=candidates,
        executing_instances=tuple(
            _executing_view(item) for item in shown_executing_instances(diagnosis)
        ),
        leader_instance_resolution=instance.resolution if instance else None,
        leader_instance=instance.name if instance else None,
        leader_instance_uid=instance.uid if instance else None,
        timing=_timing_view(trace.timing) if trace and trace.timing else None,
        is_resolved=is_resolved,
        summary=diagnosis.summary,
        resolution_rationale=trace.rationale if trace else None,
        unresolved_dimensions=list(trace.unresolved_dimensions) if trace else [],
        causal_path=[_hop_view(hop) for hop in diagnosis.causal_path],
        causal_explanation=diagnosis.causal_explanation,
        initiating_findings=[_finding_view(item) for item in initiating],
        supporting_findings=[_finding_view(item) for item in supporting],
        contradictory_findings=[_finding_view(item) for item in contradictory],
        evidence=[_finding_view(item) for item in diagnosis.evidence],
        competing_hypotheses=competing,
        alternatives=[_candidate_view(item) for item in diagnosis.alternatives],
        remediation=[
            RemediationView(
                action=item.action,
                command=item.command,
                risk=item.risk,
                requires_approval=item.requires_approval,
            )
            for item in diagnosis.remediation
        ],
        steps=[StepView(actor=s.actor, action=s.action, detail=s.detail) for s in diagnosis.steps],
        investigation_audit=(
            InvestigationAuditView(
                diagnosis_run_id=diagnosis_run_id or "",
                artifact_version=artifact_version or "",
                initial_resolution=investigation.initial_resolution.value,
                final_resolution=investigation.final_resolution.value,
                stop_reason=investigation.stop_reason.value,
                turns=investigation.turns,
                model_calls=investigation.model_calls,
                tool_calls=investigation.tool_calls,
                action_audits=[
                    InvestigationActionAuditView(
                        turn_index=audit.turn_index,
                        gap_id=audit.action.gap_id,
                        gap_dimension=audit.gap_dimension.value if audit.gap_dimension else None,
                        missing_fact=audit.missing_fact,
                        intent_id=audit.intent_id,
                        intent_kind=audit.intent_kind,
                        action=audit.action.action,
                        capability=audit.action.capability,
                        target=(
                            audit.action.target.canonical
                            if audit.action.target is not None
                            else None
                        ),
                        action_rationale=audit.action.rationale,
                        authorization_result=audit.authorization_result,
                        authorization_reason=audit.authorization_reason,
                        backend_execution_status=audit.backend_execution_status.value,
                        observation_id=audit.observation_id,
                        observation_outcome=(
                            audit.observation_outcome.value if audit.observation_outcome else None
                        ),
                        returned_evidence_refs=list(audit.returned_evidence_refs),
                        new_evidence_refs=list(audit.new_evidence_refs),
                        already_known_refs=list(audit.already_known_refs),
                        normalized_finding_ids=list(audit.normalized_finding_ids),
                        affected_hypothesis_ids=list(audit.affected_hypothesis_ids),
                        resolution_before=audit.resolution_before.value,
                        resolution_after=(
                            audit.resolution_after.value if audit.resolution_after else None
                        ),
                        decision_state_changed=audit.decision_state_changed,
                        progress_classification=audit.progress_classification,
                    )
                    for audit in investigation.action_audits
                ],
            )
            if investigation is not None
            else None
        ),
        services=list(symptoms.services),
        alert_names=list(symptoms.alert_names),
        onset=symptoms.onset,
        background_alerts_ignored=sum(symptoms.background_alert_counts.values()),
        model_calls=diagnosis.model_calls,
        mode=diagnosis.mode,
    )


def _seconds(earlier: datetime | None, later: datetime | None) -> float | None:
    if earlier is None or later is None:
        return None
    delta = (later - earlier).total_seconds()
    return delta if delta >= 0 else None


def timeline_view(
    *,
    incident: Incident,
    diagnosis: Diagnosis | None,
    run_id: str | None,
    events: list[IncidentEvent],
    alerts: list[Alert],
    diagnosis_ready: datetime | None,
) -> TimelineView:
    """Assemble the bound lifecycle timeline for one incident."""
    phases = diagnosis_phases(events, run_id)
    note = (
        "Timeline unavailable or incomplete for this diagnosis run."
        if run_id and not phases
        else newer_run_note(events, phases)
    )
    origin = phases[0].at if phases else None
    alert_fired = min((alert.starts_at for alert in alerts), default=None)
    return TimelineView(
        run_id=run_id,
        phases=[
            LifecyclePhaseView(
                name=phase.name,
                at=phase.at,
                detail=phase.detail,
                offset_seconds=_seconds(origin, phase.at) or 0.0,
            )
            for phase in phases
        ],
        note=note,
        alert_fired=alert_fired,
        incident_opened=incident.created_at,
        diagnosis_ready=diagnosis_ready,
        alert_to_diagnosis_seconds=_seconds(alert_fired or incident.created_at, diagnosis_ready),
        reads=len(diagnosis.steps) if diagnosis else 0,
        evidence_count=len(diagnosis.evidence) if diagnosis else 0,
        model_calls=diagnosis.model_calls if diagnosis else 0,
        events=[
            TimelineEventView(event_type=event.event_type.value, timestamp=event.timestamp)
            for event in events
        ],
    )


def evidence_view(evidence: Evidence) -> EvidenceView:
    return EvidenceView(
        evidence_id=str(evidence.evidence_id),
        source_type=evidence.source_type.value,
        source_system=evidence.source_system,
        collected_at=evidence.collected_at,
        starts_at=evidence.time_window.starts_at,
        ends_at=evidence.time_window.ends_at,
        raw_result_reference=evidence.raw_result_reference,
        observation=evidence.observation,
    )


def incident_list_item(incident: Incident, view: dict[str, Any] | None) -> IncidentListItem:
    """Combine an incident with its latest diagnosis view for list rendering."""
    now = datetime.now(incident.created_at.tzinfo)
    services = view.get("services") if view else None
    service = services[0] if isinstance(services, (tuple, list)) and services else None
    listed = presented_leader(
        str(view["root_cause"]) if view and view.get("root_cause") else None,
        str(view["leading_actor_display"]) if view and view.get("leading_actor_display") else None,
        tuple(str(c) for c in (view.get("leading_actor_candidates") or ())) if view else (),
        str(view["leading_actor_withheld_reason"])
        if view and view.get("leading_actor_withheld_reason")
        else None,
    )
    return IncidentListItem(
        incident_id=str(incident.incident_id),
        title=incident.title,
        status=incident.status.value,
        severity=incident.severity.value,
        source=incident.source.value,
        service=service if isinstance(service, str) else None,
        leading_root_actor=listed[2],
        leading_actor_withheld_reason=(
            str(view["leading_actor_withheld_reason"])
            if view and view.get("leading_actor_withheld_reason")
            else None
        ),
        leading_actor_display=listed[0],
        leading_actor_candidates=listed[1],
        confidence=str(view["confidence"]) if view and view.get("confidence") else None,
        resolution=str(view["resolution"]) if view and view.get("resolution") else None,
        has_diagnosis=view is not None,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        age_seconds=max((now - incident.created_at).total_seconds(), 0.0),
    )


def report_summary(snapshot: ReportSnapshot) -> ReportSummary:
    return ReportSummary(
        report_id=snapshot.report_id,
        incident_id=snapshot.incident_id,
        title=snapshot.title,
        severity=snapshot.severity,
        confidence=snapshot.confidence,
        resolution=snapshot.resolution,
        root_actor=snapshot.root_actor,
        leading_root_actor=snapshot.leading_root_actor,
        diagnosis_run_id=snapshot.diagnosis_run_id,
        report_version=snapshot.report_version,
        generated_at=snapshot.generated_at,
    )


def _change_namespace(record: ChangeRecord) -> str | None:
    """Expose only consistent namespace metadata explicitly retained in snapshots."""
    namespaces: set[str] = set()
    for snapshot in (record.before, record.after):
        metadata = snapshot.get("metadata")
        if not isinstance(metadata, dict):
            continue
        namespace = metadata.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            continue
        if metadata.get("name", record.resource_name) != record.resource_name:
            return None
        if snapshot.get("kind", record.resource_type) != record.resource_type:
            return None
        namespaces.add(namespace)
    return next(iter(namespaces)) if len(namespaces) == 1 else None


def change_view(
    record: ChangeRecord,
    onset: datetime | None = None,
    leading_actor: EntityRef | None = None,
) -> ChangeView:
    if onset is None:
        delta: float | None = None
    elif record.timestamp >= onset:
        delta = (record.timestamp - onset).total_seconds()
    else:
        delta = -abs((onset - record.timestamp).total_seconds())
    # Match on kind and name so a like-named resource of a different kind is not
    # falsely flagged. ChangeRecord carries no namespace, so namespace cannot be
    # compared; the mark stays a label, never a causal claim.
    matches = bool(
        leading_actor
        and record.resource_type == leading_actor.kind
        and record.resource_name == leading_actor.name
    )
    return ChangeView(
        change_id=str(record.change_id),
        timestamp=record.timestamp,
        resource_type=record.resource_type,
        resource_name=record.resource_name,
        namespace=_change_namespace(record),
        change_type=record.change_type.value,
        scope=record.scope.value,
        revision=record.revision,
        source=record.source,
        onset_delta_seconds=delta,
        matches_leading_actor=matches,
    )
