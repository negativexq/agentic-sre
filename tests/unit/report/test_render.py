"""Markdown and PDF renderers project the snapshot faithfully."""

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
    ResolutionTrace,
    Symptoms,
)
from packages.rca.report import LifecyclePhase
from packages.rca.resolution import resolve_hypotheses
from packages.report import ReportSnapshot, build_report, to_markdown, to_pdf

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _snapshot(resolution: Resolution, trace: ResolutionTrace | None = None) -> ReportSnapshot:
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
    diagnosis = Diagnosis(
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
        resolution_trace=trace,
    )
    return build_report(
        incident_id="i1",
        title="OrderDependencyLatencyHigh",
        severity="CRITICAL",
        status="INVESTIGATING",
        diagnosis=diagnosis,
        run_id="run-1",
        alert_fired=T0 - timedelta(seconds=2),
        incident_opened=T0,
        diagnosed_at=T0 + timedelta(seconds=24),
        phases=(
            LifecyclePhase(name="Diagnosis stored", at=T0 + timedelta(seconds=24), detail="x"),
        ),
        evidence_count=1,
        generated_at=T0 + timedelta(minutes=1),
    )


def test_markdown_contains_headline_facts() -> None:
    markdown = to_markdown(_snapshot(Resolution.RESOLVED))
    assert "# Incident report — OrderDependencyLatencyHigh" in markdown
    assert "sre-demo/Deployment/payment-service" in markdown
    assert "resolution RESOLVED" in markdown
    assert "SPEC_CHANGE" in markdown
    assert "run-1" in markdown


def test_markdown_flags_ambiguous_as_honest() -> None:
    markdown = to_markdown(_snapshot(Resolution.AMBIGUOUS))
    assert "not a wrong answer" in markdown
    assert "Leading root actor" in markdown


def test_pdf_is_a_real_pdf_document() -> None:
    pdf = to_pdf(_snapshot(Resolution.RESOLVED))
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1000


def test_eliminated_alternatives_show_rule_evidence_and_preconditions() -> None:
    late_actor = EntityRef(kind="Deployment", name="late", namespace="sre-demo")
    late = Finding(
        kind=FindingKind.IMAGE_CHANGE,
        entity=late_actor,
        at=T0 + timedelta(hours=2),
        incident_onset=T0,
        onset_delta_seconds=7200,
        temporal_role=EvidenceTemporalRole.CONSEQUENCE,
        summary="late image change",
        evidence_ids=("late-change",),
        details={"previous_observed_at": (T0 + timedelta(hours=1)).isoformat()},
    )
    contradicted = Hypothesis(
        hypothesis_id="h-late",
        causal_actor=late_actor,
        findings=(late,),
        contradictory_findings=(late,),
        causal_explanation="PATH",
    )
    snapshot = _snapshot(Resolution.RESOLVED, trace=resolve_hypotheses((contradicted,)))

    (item,) = snapshot.eliminations
    assert item.actor == "sre-demo/Deployment/late"
    assert item.rule == "m16.temporal-contradiction.v1"
    markdown = to_markdown(snapshot)
    assert "## Why not the others" in markdown
    assert "EXPLICIT_TEMPORAL_CONTRADICTION" in markdown
    assert "`late-change`" in markdown
    assert "definitely_late_beyond_onset_grace`: pass" in markdown
    assert to_pdf(snapshot).startswith(b"%PDF-")
