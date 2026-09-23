"""Render a shareable email from an immutable report snapshot.

The email is produced from the same snapshot as the PDF and Markdown exports, so
what is shared matches what the console shows. Rendering never recomputes a
causal claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from packages.report.model import ReportSnapshot
from packages.report.render import to_markdown, to_pdf


@dataclass(frozen=True)
class EmailPayload:
    """A rendered email ready for a delivery backend."""

    subject: str
    text_body: str
    html_body: str
    pdf: bytes | None
    pdf_filename: str | None


def _subject(snapshot: ReportSnapshot) -> str:
    actor = snapshot.root_actor or snapshot.leading_root_actor or snapshot.title
    return f"[Agentic SRE] {snapshot.severity} — {actor}"


def _html(snapshot: ReportSnapshot) -> str:
    actor_label = "Root cause" if snapshot.is_resolved else "Leading root actor"
    actor = snapshot.root_actor if snapshot.is_resolved else snapshot.leading_root_actor
    rows = [
        ("Severity / status", f"{snapshot.severity} / {snapshot.status}"),
        (actor_label, actor or "none identified"),
        ("Confidence", snapshot.confidence),
        ("Resolution", snapshot.resolution),
        ("Services", ", ".join(snapshot.affected_services) or "—"),
    ]
    table = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>{escape(key)}</td>"
        f"<td style='padding:4px 0'><strong>{escape(value)}</strong></td></tr>"
        for key, value in rows
    )
    honesty = (
        ""
        if snapshot.is_resolved
        else (
            "<p style='color:#555;font-size:13px'>Resolution is not RESOLVED: the engine has a "
            "leading actor but could not positively exclude a structural alternative. This is an "
            "honest epistemic state, not a wrong answer.</p>"
        )
    )
    return (
        "<div style='font-family:system-ui,sans-serif;max-width:640px'>"
        f"<h2 style='margin:0 0 4px'>{escape(snapshot.title)}</h2>"
        f"<p style='color:#888;font-size:12px;margin:0 0 12px'>Report {escape(snapshot.report_id)} "
        f"· v{escape(snapshot.report_version)} · run {escape(snapshot.diagnosis_run_id or '—')}</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>{table}</table>"
        f"<p style='font-size:14px'>{escape(snapshot.summary)}</p>"
        f"{honesty}"
        "</div>"
    )


def render_email(snapshot: ReportSnapshot, *, include_pdf: bool = True) -> EmailPayload:
    """Render subject, text and HTML bodies, and optionally attach the PDF."""
    return EmailPayload(
        subject=_subject(snapshot),
        text_body=to_markdown(snapshot),
        html_body=_html(snapshot),
        pdf=to_pdf(snapshot) if include_pdf else None,
        pdf_filename=f"report-{snapshot.report_id}.pdf" if include_pdf else None,
    )
