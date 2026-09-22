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
    Hypothesis,
    Resolution,
    Symptoms,
)
from packages.rca.report import LifecyclePhase
from packages.report import ReportSnapshot, build_report

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


def _build(resolution: Resolution) -> ReportSnapshot:
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
