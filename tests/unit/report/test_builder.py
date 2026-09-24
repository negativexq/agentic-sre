"""The report builder is a deterministic, honest projection of a diagnosis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.model import (
    CausalHop,
    Confidence,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    GapOutcomeKind,
    Hypothesis,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationExecutionStatus,
    InvestigationResult,
    InvestigationStopReason,
    Resolution,
    Symptoms,
)
from packages.rca.report import LifecyclePhase
from packages.report import ReportSnapshot, build_report, to_markdown, to_pdf

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _diagnosis(resolution: Resolution) -> Diagnosis:
    actor = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    symptom = EntityRef(kind="Deployment", name="order-service", namespace="sre-demo")
    finding = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=actor,
        at=T0 - timedelta(seconds=30),
        summary="payment-service image changed",
        evidence_ids=("obj-1",),
        temporal_role=EvidenceTemporalRole.INITIATING,
        onset_delta_seconds=-30.0,
    )
    return Diagnosis(
        incident_id="i1",
        root_cause=actor,
        confidence=Confidence.VERIFIED,
        resolution=resolution,
        summary="payment change initiated order latency",
        symptoms=Symptoms(
            onset=T0,
            last_seen=T0,
            services=("order-service",),
            namespaces=("sre-demo",),
            alert_names=("OrderDependencyLatencyHigh",),
        ),
        evidence=(finding,),
        causal_path=(CausalHop(source=actor, relation="dependency", target=symptom),),
        hypothesis=Hypothesis(
            hypothesis_id="h1", causal_actor=actor, initiating_findings=(finding,)
        ),
    )


def _build(
    resolution: Resolution, investigation: InvestigationResult | None = None
) -> ReportSnapshot:
    phases = (
        LifecyclePhase(name="Diagnosis started", at=T0, detail="reasoning begins"),
        LifecyclePhase(
            name="Diagnosis stored", at=T0 + timedelta(seconds=24), detail="stored · RESOLVED"
        ),
    )
    return build_report(
        incident_id="i1",
        title="OrderDependencyLatencyHigh",
        severity="CRITICAL",
        status="INVESTIGATING",
        diagnosis=_diagnosis(resolution),
        run_id="run-1",
        alert_fired=T0 - timedelta(seconds=2),
        incident_opened=T0,
        diagnosed_at=T0 + timedelta(seconds=24),
        phases=phases,
        evidence_count=1,
        generated_at=T0 + timedelta(minutes=1),
        investigation=investigation,
    )


def test_report_projects_resolved_diagnosis() -> None:
    report = _build(Resolution.RESOLVED)
    assert report.diagnosis_run_id == "run-1"
    assert report.root_actor == "sre-demo/Deployment/payment-service"
    assert report.leading_root_actor == report.root_actor
    assert report.is_resolved is True
    assert report.causal_path[0].relation == "dependency"
    assert report.initiating_findings[0].kind == "SPEC_CHANGE"
    assert report.diagnosis_duration_seconds == 24.0
    assert report.lifecycle[-1].offset_seconds == 24.0
    assert report.affected_services == ("order-service",)


def test_ambiguous_report_withholds_root_actor() -> None:
    report = _build(Resolution.AMBIGUOUS)
    assert report.is_resolved is False
    assert report.leading_root_actor == "sre-demo/Deployment/payment-service"
    assert report.root_actor is None


def test_builder_is_deterministic_apart_from_identity() -> None:
    a = _build(Resolution.RESOLVED).model_dump()
    b = _build(Resolution.RESOLVED).model_dump()
    for volatile in ("report_id", "generated_at"):
        a.pop(volatile)
        b.pop(volatile)
    assert a == b


def test_builder_projects_recorded_investigation_without_recomputing_it() -> None:
    initial = _diagnosis(Resolution.AMBIGUOUS)
    final = _diagnosis(Resolution.RESOLVED)
    target = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    audit = InvestigationActionAudit(
        turn_index=1,
        action=InvestigationAction(
            action="inspect",
            gap_id="gap-1",
            capability="events",
            target=target,
            rationale="check the authorized event sequence",
        ),
        intent_id="intent-1",
        intent_kind="EVENT_SEQUENCE",
        authorization_result="AUTHORIZED",
        authorization_reason="authorized by the selected gap",
        backend_execution_status=InvestigationExecutionStatus.SUCCEEDED,
        observation_id="obs-1",
        observation_outcome=GapOutcomeKind.SUPPORTS,
        returned_evidence_refs=("event:new",),
        new_evidence_refs=("event:new",),
        normalized_finding_ids=("CONFIG_CHANGE:payment-service:event:new",),
        resolution_before=Resolution.AMBIGUOUS,
        resolution_after=Resolution.RESOLVED,
        decision_state_changed=True,
        progress_classification="DECISION_STATE_CHANGED",
    )
    investigation = InvestigationResult(
        diagnosis=final,
        initial_diagnosis=initial,
        initial_resolution=Resolution.AMBIGUOUS,
        final_resolution=Resolution.RESOLVED,
        turns=1,
        tool_calls=1,
        unique_observations=1,
        unique_evidence_added=1,
        stop_reason=InvestigationStopReason.RESOLVED,
        action_audits=(audit,),
    )

    report = _build(Resolution.RESOLVED, investigation)

    assert report.report_version == "2.1"
    assert report.investigation_summary is not None
    assert report.investigation_summary.initial_resolution == "AMBIGUOUS"
    assert report.investigation_summary.stop_reason == "RESOLVED"
    assert report.investigation_timeline[0].authorization_result == "AUTHORIZED"
    assert (
        report.investigation_timeline[0].action_rationale == "check the authorized event sequence"
    )
    assert report.decision_relevant_observations[0].observation_id == "obs-1"
    assert report.non_contributing_observations == ()
    assert report.agent_safety_audit is not None
    assert report.agent_safety_audit.executed_reads == 1
    assert report.agent_safety_audit.out_of_policy_executions == 0
    assert report.agent_contribution is not None
    assert report.agent_contribution.resolution_changed is True


def test_legacy_v1_report_loads_without_investigation_sections() -> None:
    document = _build(Resolution.RESOLVED).model_dump(mode="json")
    document["report_version"] = "1.0"
    for field in (
        "investigation_summary",
        "investigation_timeline",
        "decision_relevant_observations",
        "non_contributing_observations",
        "remaining_information_gaps",
        "agent_safety_audit",
        "agent_contribution",
    ):
        document.pop(field)

    report = ReportSnapshot.model_validate(document)

    assert report.report_version == "1.0"
    assert report.investigation_summary is None
    assert report.investigation_timeline == ()
    assert "Investigation Summary" not in to_markdown(report)
    assert to_pdf(report).startswith(b"%PDF-")
