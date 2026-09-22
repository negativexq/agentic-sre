"""Render an immutable report snapshot to Markdown and PDF.

Both renderers read only the snapshot, so every export of the same report is
byte-stable in content and says the same thing about root actor, confidence and
resolution. Nothing is recomputed at render time.
"""

from __future__ import annotations

from datetime import datetime

from fpdf import FPDF

from packages.report.model import ReportFinding, ReportSnapshot


def _clock(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "—"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f} min"


def _actor_line(snapshot: ReportSnapshot) -> str:
    if snapshot.is_resolved:
        return f"Root cause: `{snapshot.root_actor}`"
    return f"Leading root actor: `{snapshot.leading_root_actor or 'none identified'}`"


def _findings_table(findings: tuple[ReportFinding, ...]) -> list[str]:
    if not findings:
        return ["_None recorded._"]
    lines = ["| Signal | When | Actor | Summary |", "| --- | --- | --- | --- |"]
    for finding in findings:
        when = _clock(finding.at)
        lines.append(f"| {finding.kind} | {when} | `{finding.entity}` | {finding.summary} |")
    return lines


def to_markdown(snapshot: ReportSnapshot) -> str:
    """Render the report as Markdown: executive summary then technical RCA."""
    lines: list[str] = []
    lines.append(f"# Incident report — {snapshot.title}")
    lines.append("")
    lines.append(
        f"**Report** `{snapshot.report_id}` · v{snapshot.report_version} · "
        f"generated {_clock(snapshot.generated_at)}"
    )
    lines.append(f"Bound to diagnosis run `{snapshot.diagnosis_run_id or 'unknown'}`")
    lines.append("")

    lines.append("## Executive summary")
    lines.append("")
    lines.append(f"- **Severity / status:** {snapshot.severity} · {snapshot.status}")
    lines.append(
        f"- **{_actor_line(snapshot)}** — confidence {snapshot.confidence}, "
        f"resolution {snapshot.resolution}"
    )
    if not snapshot.is_resolved:
        lines.append(
            "- _Resolution is not RESOLVED: the engine has a leading actor but could not "
            "positively exclude a structural alternative. This is an honest epistemic state, "
            "not a wrong answer._"
        )
    lines.append(f"- **Affected services:** {', '.join(snapshot.affected_services) or '—'}")
    lines.append(
        f"- **Timing:** started {_clock(snapshot.incident_started_at)}, "
        f"diagnosed {_clock(snapshot.diagnosed_at)} "
        f"({_duration(snapshot.diagnosis_duration_seconds)})"
    )
    lines.append("")
    lines.append(f"> {snapshot.summary}")
    lines.append("")
    if snapshot.resolution_rationale:
        lines.append(f"**Resolution rationale.** {snapshot.resolution_rationale}")
        lines.append("")

    lines.append("## Causal path")
    lines.append("")
    if snapshot.causal_path:
        for hop in snapshot.causal_path:
            lines.append(f"- `{hop.source}` —{hop.relation}→ `{hop.target}`")
    else:
        lines.append("_Evidence attaches directly to the symptom; no multi-hop path._")
    lines.append("")

    lines.append("## Why this actor")
    lines.append("")
    for label, findings in (
        ("Initiating", snapshot.initiating_findings),
        ("Supporting", snapshot.supporting_findings),
        ("Contradictory", snapshot.contradictory_findings),
    ):
        if findings:
            lines.append(f"### {label}")
            lines.extend(_findings_table(findings))
            lines.append("")

    if snapshot.alternatives:
        lines.append("## Competing hypotheses")
        lines.append("")
        for alternative in snapshot.alternatives:
            state = alternative.epistemic_state or (
                f"score {alternative.score:.1f}" if alternative.score is not None else "—"
            )
            lines.append(f"- `{alternative.actor}` ({state}) — {alternative.note}")
        lines.append("")

    lines.append("## Evidence")
    lines.append("")
    lines.extend(_findings_table(snapshot.evidence))
    lines.append("")

    if snapshot.lifecycle:
        lines.append("## Lifecycle")
        lines.append("")
        lines.append("| Stage | At | Offset | Detail |")
        lines.append("| --- | --- | --- | --- |")
        for phase in snapshot.lifecycle:
            lines.append(
                f"| {phase.name} | {_clock(phase.at)} | "
                f"T+{_duration(phase.offset_seconds)} | {phase.detail} |"
            )
        lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- **Mode:** {snapshot.mode}")
    lines.append(f"- **Model calls:** {snapshot.model_calls}")
    lines.append(f"- **Evidence rows:** {snapshot.evidence_count}")
    lines.append(f"- **Alerts:** {', '.join(snapshot.alert_names) or '—'}")
    lines.append("")
    return "\n".join(lines)


_ARROWS = {"→": "->", "—": "-", "–": "-", "•": "-", "·": "-", "≠": "!=", "`": ""}


def _latin1(text: str) -> str:
    """Make text safe for fpdf2 core fonts (latin-1 only)."""
    for source, target in _ARROWS.items():
        text = text.replace(source, target)
    return text.encode("latin-1", "replace").decode("latin-1")


class _ReportPdf(FPDF):  # type: ignore[misc]  # fpdf2 ships no type stubs
    def header(self) -> None:  # noqa: D401 - fpdf hook
        pass

    def h1(self, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("helvetica", "B", 16)
        self.multi_cell(self.epw, 8, _latin1(text))
        self.ln(1)

    def h2(self, text: str) -> None:
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("helvetica", "B", 12)
        self.multi_cell(self.epw, 7, _latin1(text))

    def body(self, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("helvetica", "", 10)
        self.multi_cell(self.epw, 5, _latin1(text))

    def kv(self, key: str, value: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("helvetica", "B", 10)
        self.multi_cell(self.epw, 5, _latin1(f"{key}: {value}"))


def to_pdf(snapshot: ReportSnapshot) -> bytes:
    """Render the report as a self-contained PDF from the same snapshot."""
    pdf = _ReportPdf()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.h1(f"Incident report — {snapshot.title}")
    pdf.set_x(pdf.l_margin)
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(
        pdf.epw,
        5,
        _latin1(
            f"Report {snapshot.report_id} · v{snapshot.report_version} · "
            f"generated {_clock(snapshot.generated_at)} · run {snapshot.diagnosis_run_id or '—'}"
        ),
    )
    pdf.set_text_color(0, 0, 0)

    pdf.h2("Executive summary")
    pdf.kv("Severity / status", f"{snapshot.severity} / {snapshot.status}")
    actor = snapshot.root_actor if snapshot.is_resolved else snapshot.leading_root_actor
    label = "Root cause" if snapshot.is_resolved else "Leading root actor"
    pdf.kv(label, actor or "none identified")
    pdf.kv("Confidence", snapshot.confidence)
    pdf.kv("Resolution", snapshot.resolution)
    pdf.kv("Services", ", ".join(snapshot.affected_services) or "—")
    pdf.kv("Diagnosis time", _duration(snapshot.diagnosis_duration_seconds))
    pdf.ln(1)
    pdf.body(snapshot.summary)
    if not snapshot.is_resolved:
        pdf.ln(1)
        pdf.set_text_color(90, 90, 90)
        pdf.body(
            "Resolution is not RESOLVED: the engine has a leading actor but could not "
            "positively exclude a structural alternative. This is an honest epistemic "
            "state, not a wrong answer."
        )
        pdf.set_text_color(0, 0, 0)

    pdf.h2("Causal path")
    if snapshot.causal_path:
        for hop in snapshot.causal_path:
            pdf.body(f"{hop.source}  --{hop.relation}-->  {hop.target}")
    else:
        pdf.body("Evidence attaches directly to the symptom; no multi-hop path.")

    for heading, findings in (
        ("Initiating evidence", snapshot.initiating_findings),
        ("Supporting evidence", snapshot.supporting_findings),
        ("Contradictory evidence", snapshot.contradictory_findings),
        ("Evidence", snapshot.evidence),
    ):
        if findings:
            pdf.h2(heading)
            for finding in findings:
                pdf.body(f"[{finding.kind}] {finding.entity}")
                pdf.body(f"    {finding.summary}")

    if snapshot.alternatives:
        pdf.h2("Competing hypotheses")
        for alternative in snapshot.alternatives:
            state = alternative.epistemic_state or (
                f"score {alternative.score:.1f}" if alternative.score is not None else "—"
            )
            pdf.body(f"{alternative.actor} ({state})")
            if alternative.note:
                pdf.body(f"    {alternative.note}")

    if snapshot.lifecycle:
        pdf.h2("Lifecycle")
        for phase in snapshot.lifecycle:
            pdf.body(f"{phase.name}  ({_clock(phase.at)}, T+{_duration(phase.offset_seconds)})")
            pdf.body(f"    {phase.detail}")

    pdf.h2("Provenance")
    pdf.kv("Mode", snapshot.mode)
    pdf.kv("Model calls", str(snapshot.model_calls))
    pdf.kv("Evidence rows", str(snapshot.evidence_count))
    pdf.kv("Alerts", ", ".join(snapshot.alert_names) or "—")

    return bytes(pdf.output())
