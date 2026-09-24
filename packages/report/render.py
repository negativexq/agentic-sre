"""Render an immutable report snapshot to Markdown and PDF.

Both renderers read only the snapshot, so every export of the same report is
byte-stable in content and says the same thing about root actor, confidence and
resolution. Nothing is recomputed at render time.
"""

from __future__ import annotations

from datetime import datetime

from fpdf import FPDF

from packages.report.model import ReportFinding, ReportInvestigationTurn, ReportSnapshot


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


def _observation_lines(turns: tuple[ReportInvestigationTurn, ...]) -> list[str]:
    if not turns:
        return ["_None recorded._"]
    lines = [
        "| Turn | Capability / target | Outcome | Decision changed |",
        "| ---: | --- | --- | --- |",
    ]
    for turn in turns:
        target = f"{turn.capability or '—'} / {turn.target or '—'}"
        lines.append(
            f"| {turn.turn_index} | {target} | {turn.observation_outcome or '—'} | "
            f"{turn.decision_state_changed} |"
        )
        lines.append(f"|  | Action rationale | {turn.action_rationale or '—'} |  |")
        if turn.normalized_finding_ids:
            lines.append(
                f"|  | Normalized Findings | {', '.join(turn.normalized_finding_ids)} |  |"
            )
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

    if snapshot.eliminations:
        lines.append("## Why not the others")
        lines.append("")
        for item in snapshot.eliminations:
            lines.append(f"### `{item.actor}` — {item.reason_code}")
            lines.append("")
            lines.append(f"- **Rule:** {item.rule or '—'} ({item.consequence or '—'})")
            lines.append(f"- **Mechanism tested:** {item.mechanism or '—'}")
            lines.append(f"- **Why:** {item.detail}")
            if item.coverage_basis:
                lines.append(f"- **Coverage:** {item.coverage_basis}")
            for check in item.preconditions:
                mark = "pass" if check.passed else "FAIL"
                lines.append(f"- **Precondition** `{check.name}`: {mark} — {check.detail}")
            lines.append(f"- **Evidence:** {', '.join(f'`{ref}`' for ref in item.evidence_ids)}")
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
    if snapshot.investigation_summary is not None:
        summary = snapshot.investigation_summary
        lines.append("## Investigation Summary")
        lines.append("")
        lines.append(f"- **Resolution:** {summary.initial_resolution} → {summary.final_resolution}")
        lines.append(
            f"- **Turns / reads / model calls:** {summary.turns} / "
            f"{summary.tool_calls} / {summary.model_calls}"
        )
        lines.append(
            f"- **Observations / new evidence refs:** {summary.unique_observations} / "
            f"{summary.unique_evidence_added}"
        )
        lines.append(f"- **Stop reason:** {summary.stop_reason}")
        lines.append("")

        lines.append("## Investigation Timeline")
        lines.append("")
        lines.append("| Turn | Action | Authorization | Backend | Outcome |")
        lines.append("| ---: | --- | --- | --- | --- |")
        for turn in snapshot.investigation_timeline:
            lines.append(
                f"| {turn.turn_index} | {turn.action} {turn.capability or ''} "
                f"{turn.target or '—'} | {turn.authorization_result}: "
                f"{turn.authorization_reason} | {turn.backend_execution_status} | "
                f"{turn.observation_outcome or '—'} |"
            )
            if turn.action_rationale:
                lines.append(f"|  | Action rationale | {turn.action_rationale} |  |  |")
        lines.append("")

        lines.append("## Decision-Relevant Observations")
        lines.append("")
        lines.extend(_observation_lines(snapshot.decision_relevant_observations))
        lines.append("")

        lines.append("## Non-Contributing Observations")
        lines.append("")
        lines.append("_No recorded decision-state transition for these observations._")
        lines.extend(_observation_lines(snapshot.non_contributing_observations))
        lines.append("")

        lines.append("## Remaining Information Gaps")
        lines.append("")
        if snapshot.remaining_information_gaps:
            for gap in snapshot.remaining_information_gaps:
                lines.append(
                    f"- {gap.gap_id} · {gap.dimension} · {gap.resolvability}: {gap.missing_fact}"
                )
        else:
            lines.append("_None recorded._")
        lines.append("")

        if snapshot.agent_safety_audit is not None:
            safety = snapshot.agent_safety_audit
            lines.append("## Agent Safety Audit")
            lines.append("")
            lines.append(f"- **Selected actions:** {safety.selected_actions}")
            lines.append(
                f"- **Authorized / rejected:** {safety.authorized_actions} / "
                f"{safety.rejected_actions}"
            )
            lines.append(f"- **Executed reads:** {safety.executed_reads}")
            lines.append(f"- **Out-of-policy executions:** {safety.out_of_policy_executions}")
            lines.append(
                f"- **Write executions / secret accesses:** {safety.write_executions} / "
                f"{safety.secret_accesses}"
            )
            lines.append("")

        if snapshot.agent_contribution is not None:
            contribution = snapshot.agent_contribution
            lines.append("## Agent Contribution")
            lines.append("")
            lines.append(f"- **Resolution changed:** {contribution.resolution_changed}")
            lines.append(f"- **Root actor changed:** {contribution.root_actor_changed}")
            lines.append(f"- **Hypotheses changed:** {contribution.hypotheses_changed}")
            lines.append(f"- **Alternatives eliminated:** {contribution.alternatives_eliminated}")
            lines.append(f"- **New evidence refs:** {contribution.new_evidence_added}")
            lines.append(f"- **Decision state changed:** {contribution.decision_state_changed}")
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

    if snapshot.eliminations:
        pdf.h2("Why not the others")
        for item in snapshot.eliminations:
            pdf.kv(item.actor, item.reason_code)
            pdf.body(f"    Rule {item.rule or '—'} ({item.consequence or '—'}), {item.mechanism}")
            pdf.body(f"    {item.detail}")
            for check in item.preconditions:
                pdf.body(f"    {check.name}: {'pass' if check.passed else 'FAIL'}")
            pdf.body(f"    Evidence: {', '.join(item.evidence_ids)}")

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

    if snapshot.investigation_summary is not None:
        summary = snapshot.investigation_summary
        pdf.h2("Investigation Summary")
        pdf.kv("Resolution", f"{summary.initial_resolution} -> {summary.final_resolution}")
        pdf.kv(
            "Turns / reads / model calls",
            f"{summary.turns} / {summary.tool_calls} / {summary.model_calls}",
        )
        pdf.kv("Stop reason", summary.stop_reason)

        pdf.h2("Investigation Timeline")
        for turn in snapshot.investigation_timeline:
            pdf.body(
                f"Turn {turn.turn_index}: {turn.action} {turn.capability or ''} "
                f"{turn.target or '—'}; {turn.authorization_result}; "
                f"{turn.backend_execution_status}; {turn.observation_outcome or '—'}"
            )
            if turn.action_rationale:
                pdf.body(f"    Action rationale: {turn.action_rationale}")

        pdf.h2("Decision-Relevant Observations")
        for turn in snapshot.decision_relevant_observations:
            pdf.body(
                f"Turn {turn.turn_index}: {turn.observation_id}; "
                f"Findings {', '.join(turn.normalized_finding_ids) or '—'}"
            )
        if not snapshot.decision_relevant_observations:
            pdf.body("None recorded.")

        pdf.h2("Non-Contributing Observations")
        for turn in snapshot.non_contributing_observations:
            pdf.body(
                f"Turn {turn.turn_index}: {turn.observation_id}; {turn.progress_classification}"
            )
        if not snapshot.non_contributing_observations:
            pdf.body("None recorded.")

        pdf.h2("Remaining Information Gaps")
        for gap in snapshot.remaining_information_gaps:
            pdf.body(f"{gap.gap_id} · {gap.dimension} · {gap.resolvability}: {gap.missing_fact}")
        if not snapshot.remaining_information_gaps:
            pdf.body("None recorded.")

        if snapshot.agent_safety_audit is not None:
            safety = snapshot.agent_safety_audit
            pdf.h2("Agent Safety Audit")
            pdf.kv(
                "Authorized / rejected",
                f"{safety.authorized_actions} / {safety.rejected_actions}",
            )
            pdf.kv("Executed reads", str(safety.executed_reads))
            pdf.kv(
                "Out-of-policy / write / secret",
                f"{safety.out_of_policy_executions} / {safety.write_executions} / "
                f"{safety.secret_accesses}",
            )

        if snapshot.agent_contribution is not None:
            contribution = snapshot.agent_contribution
            pdf.h2("Agent Contribution")
            pdf.kv("Resolution changed", str(contribution.resolution_changed))
            pdf.kv("Root actor changed", str(contribution.root_actor_changed))
            pdf.kv("Hypotheses changed", str(contribution.hypotheses_changed))
            pdf.kv("Alternatives eliminated", str(contribution.alternatives_eliminated))
            pdf.kv("New evidence refs", str(contribution.new_evidence_added))
            pdf.kv("Decision state changed", str(contribution.decision_state_changed))

    return bytes(pdf.output())
