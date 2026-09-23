"""Deterministically assemble a report snapshot from a stored diagnosis."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from packages.rca.model import (
    Candidate,
    CausalHop,
    Diagnosis,
    Finding,
    Hypothesis,
    InvestigationActionAudit,
    InvestigationResult,
    ResolutionTrace,
)
from packages.rca.report import LifecyclePhase
from packages.report.model import (
    REPORT_VERSION,
    ReportAgentContribution,
    ReportAgentSafetyAudit,
    ReportAlternative,
    ReportFinding,
    ReportHop,
    ReportInvestigationGap,
    ReportInvestigationState,
    ReportInvestigationSummary,
    ReportInvestigationTurn,
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


def _investigation_states(diagnosis: Diagnosis | None) -> tuple[ReportInvestigationState, ...]:
    if diagnosis is None:
        return ()
    epistemic = _epistemic_states(diagnosis.resolution_trace)
    hypotheses = (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *(diagnosis.alternative_hypotheses or diagnosis.ambiguous_hypotheses),
    )
    unique: dict[str, ReportInvestigationState] = {}
    for hypothesis in hypotheses:
        unique[hypothesis.hypothesis_id] = ReportInvestigationState(
            hypothesis_id=hypothesis.hypothesis_id,
            actor=hypothesis.causal_actor.canonical,
            state=epistemic.get(hypothesis.hypothesis_id, "UNRESOLVED"),
        )
    return tuple(unique[key] for key in sorted(unique))


def _investigation_gaps(diagnosis: Diagnosis | None) -> tuple[ReportInvestigationGap, ...]:
    if diagnosis is None:
        return ()
    return tuple(
        ReportInvestigationGap(
            gap_id=gap.gap_id,
            dimension=gap.dimension.value,
            missing_fact=gap.missing_fact,
            resolvability=gap.resolvability.value,
        )
        for gap in sorted(diagnosis.information_gaps, key=lambda item: item.gap_id)
    )


def _investigation_turn(audit: InvestigationActionAudit) -> ReportInvestigationTurn:
    action = audit.action
    return ReportInvestigationTurn(
        turn_index=audit.turn_index,
        gap_id=action.gap_id,
        gap_dimension=audit.gap_dimension.value if audit.gap_dimension else None,
        missing_fact=audit.missing_fact,
        intent_id=audit.intent_id,
        intent_kind=audit.intent_kind,
        action=action.action,
        capability=action.capability,
        target=action.target.canonical if action.target is not None else None,
        bounded_query=action.query.model_dump(mode="json") if action.query is not None else None,
        action_rationale=action.rationale,
        authorization_result=audit.authorization_result,
        authorization_reason=audit.authorization_reason,
        backend_execution_status=audit.backend_execution_status.value,
        observation_id=audit.observation_id,
        observation_outcome=(
            audit.observation_outcome.value if audit.observation_outcome else None
        ),
        returned_evidence_refs=audit.returned_evidence_refs,
        new_evidence_refs=audit.new_evidence_refs,
        already_known_refs=audit.already_known_refs,
        normalized_finding_ids=audit.normalized_finding_ids,
        affected_hypothesis_ids=audit.affected_hypothesis_ids,
        resolution_before=audit.resolution_before.value,
        resolution_after=audit.resolution_after.value if audit.resolution_after else None,
        hypothesis_states_before=tuple(
            ReportInvestigationState(
                hypothesis_id=item.hypothesis_id,
                actor=item.actor.canonical,
                state=item.state.value,
            )
            for item in audit.hypothesis_states_before
        ),
        hypothesis_states_after=tuple(
            ReportInvestigationState(
                hypothesis_id=item.hypothesis_id,
                actor=item.actor.canonical,
                state=item.state.value,
            )
            for item in audit.hypothesis_states_after
        ),
        gap_states_before=tuple(
            ReportInvestigationGap(
                gap_id=item.gap_id,
                dimension=item.dimension.value,
                missing_fact=item.missing_fact,
                resolvability=item.resolvability.value,
            )
            for item in audit.gap_states_before
        ),
        gap_states_after=tuple(
            ReportInvestigationGap(
                gap_id=item.gap_id,
                dimension=item.dimension.value,
                missing_fact=item.missing_fact,
                resolvability=item.resolvability.value,
            )
            for item in audit.gap_states_after
        ),
        decision_state_changed=audit.decision_state_changed,
        progress_classification=audit.progress_classification,
    )


def _investigation_projection(
    investigation: InvestigationResult,
) -> tuple[
    ReportInvestigationSummary,
    tuple[ReportInvestigationTurn, ...],
    tuple[ReportInvestigationTurn, ...],
    tuple[ReportInvestigationTurn, ...],
    tuple[ReportInvestigationGap, ...],
    ReportAgentSafetyAudit,
    ReportAgentContribution,
]:
    turns = tuple(_investigation_turn(item) for item in investigation.action_audits)
    executed = tuple(
        item
        for item in investigation.action_audits
        if item.backend_execution_status.value != "NOT_EXECUTED"
    )
    initial = investigation.initial_diagnosis
    final = investigation.diagnosis
    initial_actor = (
        initial.hypothesis.causal_actor
        if initial and initial.hypothesis
        else initial.root_cause
        if initial
        else None
    )
    final_actor = final.hypothesis.causal_actor if final.hypothesis else final.root_cause
    initial_states = _investigation_states(initial)
    final_states = _investigation_states(final)
    eliminated_before = (
        set(initial.resolution_trace.eliminated_hypotheses)
        if initial and initial.resolution_trace
        else set()
    )
    eliminated_after = (
        set(final.resolution_trace.eliminated_hypotheses) if final.resolution_trace else set()
    )
    decision_changed = any(
        item.decision_state_changed is True for item in investigation.action_audits
    )
    return (
        ReportInvestigationSummary(
            initial_resolution=investigation.initial_resolution.value,
            final_resolution=investigation.final_resolution.value,
            turns=investigation.turns,
            model_calls=investigation.model_calls,
            tool_calls=investigation.tool_calls,
            unique_observations=investigation.unique_observations,
            unique_evidence_added=investigation.unique_evidence_added,
            stop_reason=investigation.stop_reason.value,
            initial_hypotheses=initial_states,
            initial_information_gaps=_investigation_gaps(initial),
        ),
        turns,
        tuple(
            turn for turn in turns if turn.observation_id and turn.decision_state_changed is True
        ),
        tuple(
            turn
            for turn in turns
            if turn.observation_id and turn.decision_state_changed is not True
        ),
        _investigation_gaps(final),
        ReportAgentSafetyAudit(
            selected_actions=len(investigation.action_audits),
            authorized_actions=sum(
                item.authorization_result == "AUTHORIZED" for item in investigation.action_audits
            ),
            rejected_actions=sum(
                item.authorization_result == "REJECTED" for item in investigation.action_audits
            ),
            executed_reads=len(executed),
            out_of_policy_executions=sum(
                item.authorization_result != "AUTHORIZED" for item in executed
            ),
            write_executions=sum(item.action.action != "inspect" for item in executed),
            secret_accesses=sum(
                (item.action.capability or "").casefold().startswith("secret") for item in executed
            ),
        ),
        ReportAgentContribution(
            resolution_changed=investigation.initial_resolution
            is not investigation.final_resolution,
            root_actor_changed=initial_actor != final_actor,
            hypotheses_changed=initial_states != final_states or decision_changed,
            alternatives_eliminated=len(eliminated_after - eliminated_before),
            new_evidence_added=investigation.unique_evidence_added,
            decision_state_changed=decision_changed,
        ),
    )


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
    investigation: InvestigationResult | None = None,
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
    investigation_projection = (
        _investigation_projection(investigation) if investigation is not None else None
    )
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
        investigation_summary=(investigation_projection[0] if investigation_projection else None),
        investigation_timeline=(investigation_projection[1] if investigation_projection else ()),
        decision_relevant_observations=(
            investigation_projection[2] if investigation_projection else ()
        ),
        non_contributing_observations=(
            investigation_projection[3] if investigation_projection else ()
        ),
        remaining_information_gaps=(
            investigation_projection[4] if investigation_projection else ()
        ),
        agent_safety_audit=(investigation_projection[5] if investigation_projection else None),
        agent_contribution=(investigation_projection[6] if investigation_projection else None),
    )
