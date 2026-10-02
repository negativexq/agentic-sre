"""Shadow of the temporal-relevance filter on supported leadership (m21 contract §11, PROPOSED).

Measurement only: it reads a stored diagnosis document and the incident's Events and says which competing
`SUPPORTED` candidates would remain eligible to lead. Nothing in the engine calls it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

CHAOS_KINDS = frozenset(
    {"NetworkChaos", "StressChaos", "PodChaos", "IOChaos", "HTTPChaos", "DNSChaos", "TimeChaos"}
)
RESOLUTION = timedelta(seconds=1)  # event-timestamp resolution tolerated, as in the strong rule
_SPEC = re.compile(r"spec changed: (?P<field>.+?): (?P<old>.*?) -> (?P<new>.*)$")


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def chaos_intervals(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], tuple[datetime | None, datetime | None]]:
    """``(kind, name) -> (Applied, Recovered)``: the first of each, from the experiments' own Events."""
    found: dict[tuple[str, str], dict[str, datetime]] = {}
    for event in events:
        involved = event.get("involvedObject") or {}
        kind, name, reason = involved.get("kind"), involved.get("name"), event.get("reason")
        if kind not in CHAOS_KINDS or reason not in ("Applied", "Recovered") or not name:
            continue
        at = (
            _time(event.get("eventTime"))
            or _time(event.get("firstTimestamp"))
            or _time(event.get("lastTimestamp"))
        )
        if at is None:
            continue
        slot = found.setdefault((kind, name), {})
        if reason not in slot or at < slot[reason]:
            slot[reason] = at
    return {key: (slots.get("Applied"), slots.get("Recovered")) for key, slots in found.items()}


@dataclass(frozen=True)
class Interval:
    start: datetime | None
    end: datetime | None  # None: no observed end, so still in effect


def effect_interval(
    hypothesis: Mapping[str, Any],
    intervals: Mapping[tuple[str, str], tuple[datetime | None, datetime | None]],
) -> Interval:
    """§11 definition 1, from observations only."""
    actor = hypothesis.get("causal_actor") or {}
    findings = sorted(
        (f for f in hypothesis.get("initiating_findings") or [] if _time(f.get("at")) is not None),
        key=lambda f: _time(f["at"]),  # type: ignore[arg-type,return-value]
    )
    if actor.get("kind") in CHAOS_KINDS:
        applied, recovered = intervals.get((actor["kind"], actor.get("name", "")), (None, None))
        start = applied or (_time(findings[0]["at"]) if findings else None)
        return Interval(start, recovered)
    start = _time(findings[0]["at"]) if findings else None
    # a specification change ends at an observed later change restoring the earlier value of the same field
    first_old: dict[str, str] = {}
    end: datetime | None = None
    for finding in findings:
        match = _SPEC.match(str(finding.get("summary") or ""))
        if finding.get("kind") != "SPEC_CHANGE" or match is None:
            end = None  # anything else keeps the actor in effect
            continue
        field, old, new = match["field"], match["old"], match["new"]
        first_old.setdefault(field, old)
        end = _time(finding["at"]) if new == first_old[field] else None
    return Interval(start, end)


def ended_before(interval: Interval, onset: datetime, window: timedelta) -> bool:
    return interval.end is not None and interval.end + window < onset


def displacing(
    hypothesis: Mapping[str, Any],
    interval: Interval,
    onset: datetime,
    window: timedelta,
) -> bool:
    """§11 definition 5: tied to the onset by an observation, never by an unknown end.

    An initiation observed within ``[T0 - W, T0]``, or an experiment observed in progress at ``T0``.
    """
    if interval.start is not None and interval.start > onset + RESOLUTION:
        return False
    initiations = [
        t
        for t in (_time(f.get("at")) for f in hypothesis.get("initiating_findings") or [])
        if t is not None
    ]
    if interval.start is not None:
        initiations.append(interval.start)
    if any(onset - window <= t <= onset + RESOLUTION for t in initiations):
        return True
    actor = hypothesis.get("causal_actor") or {}
    in_progress = (
        actor.get("kind") in CHAOS_KINDS
        and interval.start is not None
        and interval.start <= onset + RESOLUTION
        and (interval.end is None or interval.end >= onset)
    )
    return in_progress


def linked(hypothesis: Mapping[str, Any]) -> bool:
    return bool(hypothesis.get("linked_symptoms")) or bool(hypothesis.get("causal_paths"))


@dataclass(frozen=True)
class Shadow:
    display: str  # SINGLE or COMPETING among the eligible candidates; unchanged when the filter does not act
    eligible: tuple[tuple[str, str], ...]
    demoted: tuple[tuple[str, str], ...]
    acted: bool


def shadow_leadership(
    document: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    window: timedelta,
) -> Shadow:
    """The §11 filter applied to one stored diagnosis."""
    candidates = [(c["kind"], c["name"]) for c in document.get("leading_actor_candidates") or []]
    display = str(document.get("leading_actor_display") or "SINGLE")
    if display != "COMPETING" or document.get("leading_actor_tier") != "SUPPORTED":
        return Shadow(display, tuple(candidates), (), False)
    hypotheses: dict[tuple[str, str], Mapping[str, Any]] = {}
    for hypothesis in [document.get("hypothesis"), *(document.get("alternative_hypotheses") or [])]:
        if hypothesis:
            actor = hypothesis.get("causal_actor") or {}
            hypotheses.setdefault((actor.get("kind", ""), actor.get("name", "")), hypothesis)
    intervals = chaos_intervals(events)
    onsets = [_time(h.get("episode_onset")) for h in hypotheses.values()]
    onset = min((o for o in onsets if o is not None), default=None)
    if onset is None:
        return Shadow(display, tuple(candidates), (), False)
    state = {}
    for candidate in candidates:
        hypothesis = hypotheses.get(candidate, {})
        interval = effect_interval(hypothesis, intervals)
        state[candidate] = (
            ended_before(interval, onset, window),
            displacing(hypothesis, interval, onset, window) and linked(hypothesis),
        )
    if not any(not ended and live for ended, live in state.values()):
        return Shadow(display, tuple(candidates), (), False)
    eligible = tuple(c for c in candidates if not state[c][0])
    demoted = tuple(c for c in candidates if state[c][0])
    return Shadow("SINGLE" if len(eligible) == 1 else "COMPETING", eligible, demoted, bool(demoted))


def shadow_all(
    documents: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], window: timedelta
) -> list[Shadow]:
    return [shadow_leadership(document, events, window) for document in documents]
