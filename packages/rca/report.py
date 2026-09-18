"""Self-contained HTML rendering for diagnoses (web UI and static reports)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from packages.rca.model import Confidence, Diagnosis, InvestigationResult

_STYLE = """
:root { --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#e3e2dd; --card:#ffffff;
  --ok:#1f7a4d; --warn:#9a6700; --low:#a4382d; --accent:#2f5da8; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --fg:#ecebe6; --muted:#a09f99;
  --line:#2c2c29; --card:#1c1c1a; --ok:#5cc28f; --warn:#e0b44c; --low:#ec7b6f; --accent:#7ea6ea; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
a { color:var(--accent); }
.muted { color:var(--muted); }
p, td { overflow-wrap:anywhere; }
td:first-child { white-space:nowrap; overflow-wrap:normal; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
.cause { font:600 17px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; word-break:break-all; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600;
  border:1px solid currentColor; }
.VERIFIED { color:var(--ok); } .LIKELY { color:var(--warn); } .UNVERIFIED { color:var(--low); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; }
code, pre { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px;
  overflow-x:auto; white-space:pre-wrap; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.table-wrap { overflow-x:auto; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _badge(confidence: Confidence | str) -> str:
    value = confidence.value if isinstance(confidence, Confidence) else str(confidence)
    return f"<span class='badge {escape(value)}'>{escape(value)}</span>"


def diagnosis_html(
    diagnosis: Diagnosis,
    *,
    back_link: str | None = None,
    investigation: InvestigationResult | None = None,
) -> str:
    """Render one diagnosis as a standalone page."""
    symptoms = diagnosis.symptoms
    parts: list[str] = []
    if back_link:
        parts.append(f"<p><a href='{escape(back_link)}'>&larr; All incidents</a></p>")
    parts.append(f"<h1>Incident {escape(diagnosis.incident_id)}</h1>")
    parts.append(
        f"<p class='muted'>Mode {escape(diagnosis.mode)} · {diagnosis.model_calls} model call(s)</p>"
    )
    cause = (
        escape(diagnosis.root_cause.canonical) if diagnosis.root_cause else "No root cause found"
    )
    parts.append(
        "<div class='card'>"
        f"<div class='muted'>Root cause {_badge(diagnosis.confidence)}</div>"
        f"<div class='cause'>{cause}</div>"
        f"<div class='muted'>Resolution</div><div class='badge'>{escape(diagnosis.resolution.value)}</div>"
        f"<p>{escape(diagnosis.summary)}</p></div>"
    )
    if diagnosis.resolution_trace:
        trace = diagnosis.resolution_trace
        resolution_parts = [
            f"<h2>Resolution: {escape(trace.state.value)}</h2>",
            f"<div class='card'><p>{escape(trace.rationale)}</p>",
        ]
        if trace.state.value == "AMBIGUOUS" and diagnosis.ambiguous_hypotheses:
            resolution_parts.append(
                "<div class='muted'>Leading hypotheses</div><ul>"
                + "".join(
                    f"<li><code>{escape(item.causal_actor.canonical)}</code></li>"
                    for item in diagnosis.ambiguous_hypotheses
                )
                + "</ul>"
            )
        if trace.unresolved_dimensions:
            resolution_parts.append(
                "<div class='muted'>Unresolved dimensions</div><ul>"
                + "".join(f"<li>{escape(item)}</li>" for item in trace.unresolved_dimensions)
                + "</ul>"
            )
        resolution_parts.append("</div>")
        parts.append("".join(resolution_parts))
    onset = symptoms.onset.isoformat(timespec="seconds") if symptoms.onset else "unknown"
    background = sum(symptoms.background_alert_counts.values())
    parts.append(
        "<h2>Symptoms</h2><div class='grid'>"
        f"<div class='card'><div class='muted'>Alerts</div>{escape(', '.join(symptoms.alert_names) or '-')}</div>"
        f"<div class='card'><div class='muted'>Services</div>{escape(', '.join(symptoms.services) or '-')}</div>"
        f"<div class='card'><div class='muted'>Onset</div>{escape(onset)}</div>"
        f"<div class='card'><div class='muted'>Background alerts ignored</div>{background}</div>"
        "</div>"
    )
    if diagnosis.hypothesis:
        hypothesis = diagnosis.hypothesis
        hypothesis_parts = [
            "<h2>Causal hypothesis</h2><div class='card'>"
            f"<div class='muted'>Causal actor</div><div class='cause'>{escape(hypothesis.causal_actor.canonical)}</div>"
        ]
        if hypothesis.manifestations:
            hypothesis_parts.append(
                "<div class='muted'>Manifestations</div><ul>"
                + "".join(
                    f"<li><code>{escape(entity.canonical)}</code></li>"
                    for entity in hypothesis.manifestations[:6]
                )
                + "</ul>"
            )
        for title, findings in (
            ("Initiating evidence", hypothesis.initiating_findings),
            ("Supporting evidence", hypothesis.supporting_findings),
            ("Contradictory evidence", hypothesis.contradictory_findings),
        ):
            if findings:
                hypothesis_parts.append(
                    f"<div class='muted'>{escape(title)}</div><ul>"
                    + "".join(f"<li>{escape(finding.summary)}</li>" for finding in findings[:6])
                    + "</ul>"
                )
        hypothesis_parts.append("</div>")
        parts.append("".join(hypothesis_parts))
    if diagnosis.causal_path:
        hops = "".join(
            "<div class='card'><code>"
            f"{escape(hop.source.canonical)}</code>"
            f" <span class='muted'>--{escape(hop.relation)}--&gt;</span> "
            f"<code>{escape(hop.target.canonical)}</code></div>"
            for hop in diagnosis.causal_path
        )
        parts.append(f"<h2>Why this cause / Causal path</h2><div class='grid'>{hops}</div>")
    elif diagnosis.causal_explanation == "DIRECT":
        parts.append(
            "<h2>Why this cause / Direct evidence</h2>"
            "<p class='muted'>The strongest evidence is attached directly to the symptom entity.</p>"
        )
    if diagnosis.evidence:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(f.kind.value)}</code></td>"
            f"<td>{escape(f.at.isoformat(timespec='seconds')) if f.at else '-'}</td>"
            f"<td>{escape(f.summary)}</td>"
            f"<td><code>{escape(', '.join(f.evidence_ids[:3]))}</code></td>"
            "</tr>"
            for f in diagnosis.evidence
        )
        parts.append(
            "<h2>Evidence</h2><div class='table-wrap'><table><tr><th>Signal</th><th>When</th>"
            f"<th>Observation</th><th>Source</th></tr>{rows}</table></div>"
        )
    if diagnosis.remediation:
        items = "".join(
            f"<div class='card'><strong>{escape(r.action)}</strong>"
            f"<pre>{escape(r.command)}</pre>"
            f"<div class='muted'>Risk: {escape(r.risk)} · "
            f"{'needs approval' if r.requires_approval else 'read-only'} · not executed</div></div>"
            for r in diagnosis.remediation
        )
        title = (
            "Possible remediation (hypothesis-specific)"
            if diagnosis.resolution.value == "AMBIGUOUS"
            else "Proposed remediation"
        )
        parts.append(f"<h2>{escape(title)}</h2>{items}")
    if diagnosis.alternatives:
        rows = "".join(
            f"<tr><td><code>{escape(c.entity.canonical)}</code></td><td>{c.score:.1f}</td>"
            f"<td>{escape(c.findings[0].summary) if c.findings else 'structurally plausible actor; evidence not yet observed'}</td></tr>"
            for c in diagnosis.alternatives
        )
        parts.append(
            "<h2>Alternatives</h2><div class='table-wrap'><table><tr><th>Candidate</th>"
            f"<th>Score</th><th>Strongest signal</th></tr>{rows}</table></div>"
        )
    if investigation is not None:
        observations = "".join(
            f"<li><code>{escape(item.capability)}</code> "
            f"<code>{escape(item.target.canonical)}</code> "
            f"{escape(item.outcome.value)}</li>"
            for item in investigation.observations[:12]
        )
        parts.append(
            "<h2>Bounded investigation</h2><div class='card'>"
            f"<p>Initial resolution: <code>{escape(investigation.initial_resolution.value)}</code><br>"
            f"Final resolution: <code>{escape(investigation.final_resolution.value)}</code><br>"
            f"Turns: {investigation.turns} · Tool calls: {investigation.tool_calls} · "
            f"Model calls: {investigation.model_calls}<br>"
            f"Stop reason: <code>{escape(investigation.stop_reason.value)}</code></p>"
            + (
                f"<div class='muted'>Read-only observations</div><ul>{observations}</ul>"
                if observations
                else ""
            )
            + "</div>"
        )
    steps = "".join(
        f"<tr><td>{escape(s.actor)}</td><td>{escape(s.action)}</td><td>{escape(s.detail)}</td></tr>"
        for s in diagnosis.steps
    )
    parts.append(
        "<h2>Investigation trace</h2><div class='table-wrap'><table>"
        f"<tr><th>Actor</th><th>Step</th><th>Detail</th></tr>{steps}</table></div>"
    )
    return _page(f"Diagnosis {diagnosis.incident_id}", "".join(parts))


def incidents_html(rows: Sequence[dict[str, str]]) -> str:
    """Render the incident list with each incident's latest diagnosis."""
    body = "".join(
        "<tr>"
        f"<td><a href='/incidents/{escape(r['incident_id'])}'>{escape(r['title'])}</a></td>"
        f"<td>{escape(r['status'])}</td><td>{escape(r['created_at'])}</td>"
        f"<td><code>{escape(r.get('root_cause') or '-')}</code></td>"
        f"<td>{_badge(r['confidence']) if r.get('confidence') else '-'}</td>"
        "</tr>"
        for r in rows
    )
    table = (
        "<div class='table-wrap'><table><tr><th>Incident</th><th>Status</th><th>Opened</th>"
        f"<th>Root cause</th><th>Confidence</th></tr>{body}</table></div>"
        if rows
        else "<p class='muted'>No incidents yet. Alerts arrive through the Alertmanager webhook.</p>"
    )
    return _page("Incidents", f"<h1>Incidents</h1>{table}")


def diagnosis_pending_html(incident_id: str, *, back_link: str | None = None) -> str:
    """Render a read-only state when diagnosis has not been generated yet."""
    back = f"<p><a href='{escape(back_link)}'>&larr; All incidents</a></p>" if back_link else ""
    body = (
        f"{back}<h1>Incident {escape(incident_id)}</h1>"
        "<div class='card'><h2>Diagnosis not generated yet</h2>"
        "<p>No diagnosis has been generated for this incident yet.</p>"
        "<p>Use the authenticated diagnosis API action to generate one.</p></div>"
    )
    return _page(f"Diagnosis pending {incident_id}", body)


__all__ = ["diagnosis_html", "diagnosis_pending_html", "incidents_html"]
