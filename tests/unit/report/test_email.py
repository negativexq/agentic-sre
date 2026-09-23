"""The email renderer projects the same snapshot as the other exports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.model import (
    Confidence,
    Diagnosis,
    EntityRef,
    Resolution,
    Symptoms,
)
from packages.report import ReportSnapshot, build_report
from packages.report.email import render_email

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _snapshot(resolution: Resolution) -> ReportSnapshot:
    actor = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
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
    )
    return build_report(
        incident_id="i1",
        title="OrderDependencyLatencyHigh",
        severity="CRITICAL",
        status="INVESTIGATING",
        diagnosis=diagnosis,
        run_id="run-1",
        alert_fired=T0,
        incident_opened=T0,
        diagnosed_at=T0 + timedelta(seconds=24),
        phases=(),
        evidence_count=0,
        generated_at=T0 + timedelta(minutes=1),
    )


def test_email_subject_and_bodies_from_snapshot() -> None:
    payload = render_email(_snapshot(Resolution.RESOLVED))
    assert payload.subject == "[Agentic SRE] CRITICAL — sre-demo/Deployment/payment-service"
    assert "OrderDependencyLatencyHigh" in payload.html_body
    assert "Incident report" in payload.text_body
    assert payload.pdf is not None and payload.pdf.startswith(b"%PDF-")
    assert payload.pdf_filename is not None
    assert payload.pdf_filename.startswith("report-") and payload.pdf_filename.endswith(".pdf")


def test_email_can_omit_pdf() -> None:
    payload = render_email(_snapshot(Resolution.RESOLVED), include_pdf=False)
    assert payload.pdf is None
    assert payload.pdf_filename is None


def test_ambiguous_email_states_it_is_not_wrong() -> None:
    payload = render_email(_snapshot(Resolution.AMBIGUOUS))
    assert "not a wrong answer" in payload.html_body
