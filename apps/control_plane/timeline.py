"""Bind a diagnosis run's timeline events to inspectable lifecycle phases.

Shared by the server-rendered incident page and the console API so both derive
the same phases from the same events. The invariant is that a rendered timeline
belongs to exactly the diagnosis run it is shown with (bound by ``run_id``); a
run whose completing event was lost has no phases rather than borrowing another
run's — see docs/diagnosis-timeline.md.
"""

from __future__ import annotations

from datetime import datetime

from packages.contracts import IncidentEvent, IncidentEventType
from packages.rca.report import LifecyclePhase

_PHASE_LABELS = {
    IncidentEventType.DIAGNOSIS_STARTED: "Diagnosis started",
    IncidentEventType.EVIDENCE_GATHERED: "Evidence gathered",
    IncidentEventType.RCA_ENGINE_COMPLETED: "RCA engine completed",
    IncidentEventType.DIAGNOSIS_COMPLETED: "Diagnosis stored",
}
_PHASE_ORDER = list(_PHASE_LABELS)
_PIPELINE_EVENTS = {*_PHASE_LABELS, IncidentEventType.DIAGNOSIS_FAILED}


def phase_detail(event: IncidentEvent) -> str:
    """A short human detail for one pipeline phase, from its payload."""
    payload = event.payload
    if event.event_type is IncidentEventType.EVIDENCE_GATHERED:
        return (
            f"objects {payload.get('objects', 0)} · journal {payload.get('journal', 0)} · "
            f"events {payload.get('events', 0)} · logs {payload.get('logs', 0)}"
        )
    if event.event_type is IncidentEventType.RCA_ENGINE_COMPLETED:
        return f"leading {payload.get('leading_actor') or '—'} · {payload.get('reads', 0)} reads"
    if event.event_type is IncidentEventType.DIAGNOSIS_COMPLETED:
        return f"{payload.get('root_cause') or 'no root cause'} · {payload.get('resolution', '')}"
    return "reasoning begins"


def runs(events: list[IncidentEvent]) -> dict[str, list[IncidentEvent]]:
    """Group pipeline events by their run id."""
    grouped: dict[str, list[IncidentEvent]] = {}
    for event in events:
        if event.event_type not in _PIPELINE_EVENTS:
            continue
        grouped.setdefault(str(event.payload.get("run_id", "")), []).append(event)
    return grouped


def diagnosis_phases(events: list[IncidentEvent], run_id: str | None) -> tuple[LifecyclePhase, ...]:
    """The phases of exactly the diagnosis run that produced the shown diagnosis.

    The stored diagnosis carries its own ``run_id``, so the timeline is bound to
    that run by id — not inferred from "latest completed event". If the
    completing event was lost (timeline persistence is best effort), that run
    has no phases here and the caller says the timeline is unavailable rather
    than rendering an unrelated run.
    """
    if not run_id:
        return ()
    group = runs(events).get(run_id, [])
    if not any(item.event_type is IncidentEventType.DIAGNOSIS_COMPLETED for item in group):
        return ()
    ordered = sorted(
        (item for item in group if item.event_type in _PHASE_LABELS),
        key=lambda item: _PHASE_ORDER.index(item.event_type),
    )
    return tuple(
        LifecyclePhase(
            name=_PHASE_LABELS[event.event_type], at=event.timestamp, detail=phase_detail(event)
        )
        for event in ordered
    )


def newer_run_note(
    events: list[IncidentEvent], shown_phases: tuple[LifecyclePhase, ...]
) -> str | None:
    """Flag the newest run started after the shown one that has not completed.

    So the page can say a later diagnosis is in progress or failed, instead of
    silently hiding it behind the completed run it renders.
    """
    if not shown_phases:
        return None
    shown_start = shown_phases[0].at
    newest: tuple[datetime, bool] | None = None
    for group in runs(events).values():
        started = next(
            (i for i in group if i.event_type is IncidentEventType.DIAGNOSIS_STARTED), None
        )
        if started is None or started.timestamp <= shown_start:
            continue
        kinds = {item.event_type for item in group}
        if IncidentEventType.DIAGNOSIS_COMPLETED in kinds:
            continue
        failed = IncidentEventType.DIAGNOSIS_FAILED in kinds
        if newest is None or started.timestamp > newest[0]:
            newest = (started.timestamp, failed)
    if newest is None:
        return None
    if newest[1]:
        return "A newer diagnosis run failed; showing the last completed one."
    return "A newer diagnosis run is in progress; showing the last completed one."


__all__ = ["diagnosis_phases", "newer_run_note", "phase_detail", "runs"]
