"""Temporal relevance of supported leaders (m21 causal semantics contract §11).

Decides which competing ``SUPPORTED`` candidates are eligible to lead. It is the single implementation shared by
the operator's leader projection (``presentation.py``, adopted for presentation with ``W`` = 5 minutes) and the
shadow measurement over stored diagnoses (``packages/evals/temporal_relevance.py``); both build the plain inputs
below from their own data, so what was measured is what is shown.

It never removes or weakens a claim: a candidate that is not eligible keeps its hypothesis, findings and support.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

CHAOS_KINDS = frozenset(
    {"NetworkChaos", "StressChaos", "PodChaos", "IOChaos", "HTTPChaos", "DNSChaos", "TimeChaos"}
)
RESOLUTION = timedelta(seconds=1)  # event-timestamp resolution tolerated, as in the strong rule
_SPEC = re.compile(r"spec changed: (?P<field>.+?): (?P<old>.*?) -> (?P<new>.*)$")

ActorKey = tuple[str, str]  # (kind, name)


@dataclass(frozen=True)
class FindingView:
    kind: str
    at: datetime | None
    summary: str = ""


@dataclass(frozen=True)
class CandidateView:
    """What §11 reads of one candidate's hypothesis."""

    actor: ActorKey
    onset: datetime | None
    initiating: tuple[FindingView, ...]
    linked: bool  # at least one linked symptom or causal path (definition 4)


@dataclass(frozen=True)
class ChaosObservation:
    kind: str
    name: str
    reason: str
    at: datetime | None


@dataclass(frozen=True)
class Interval:
    start: datetime | None
    end: datetime | None  # None: no observed end, so still in effect


@dataclass(frozen=True)
class Eligibility:
    eligible: tuple[ActorKey, ...]
    ineligible: tuple[ActorKey, ...]

    @property
    def acted(self) -> bool:
        return bool(self.ineligible)


def chaos_intervals(
    observations: Iterable[ChaosObservation],
) -> dict[ActorKey, tuple[datetime | None, datetime | None]]:
    """``(kind, name) -> (Applied, Recovered)``: the first of each, from the experiments' own Events."""
    found: dict[ActorKey, dict[str, datetime]] = {}
    for o in observations:
        if o.kind not in CHAOS_KINDS or o.reason not in ("Applied", "Recovered") or not o.name:
            continue
        if o.at is None:
            continue
        slot = found.setdefault((o.kind, o.name), {})
        if o.reason not in slot or o.at < slot[o.reason]:
            slot[o.reason] = o.at
    return {key: (slots.get("Applied"), slots.get("Recovered")) for key, slots in found.items()}


def effect_interval(
    candidate: CandidateView,
    intervals: Mapping[ActorKey, tuple[datetime | None, datetime | None]],
) -> Interval:
    """§11 definition 1, from observations only."""
    findings = sorted((f for f in candidate.initiating if f.at is not None), key=lambda f: f.at)  # type: ignore[arg-type,return-value]
    if candidate.actor[0] in CHAOS_KINDS:
        applied, recovered = intervals.get(candidate.actor, (None, None))
        return Interval(applied or (findings[0].at if findings else None), recovered)
    start = findings[0].at if findings else None
    # a specification change ends at an observed later change restoring the earlier value of the same field
    first_old: dict[str, str] = {}
    end: datetime | None = None
    for finding in findings:
        match = _SPEC.match(finding.summary)
        if finding.kind != "SPEC_CHANGE" or match is None:
            end = None  # anything else keeps the actor in effect
            continue
        field, old, new = match["field"], match["old"], match["new"]
        first_old.setdefault(field, old)
        end = finding.at if new == first_old[field] else None
    return Interval(start, end)


def ended_before(interval: Interval, onset: datetime, window: timedelta) -> bool:
    """§11 definition 2."""
    return interval.end is not None and interval.end + window < onset


def displacing(
    candidate: CandidateView, interval: Interval, onset: datetime, window: timedelta
) -> bool:
    """§11 definition 5: tied to the onset by an observation, never by an unknown end.

    An initiation observed within ``[T0 - W, T0]``, or an experiment observed in progress at ``T0``.
    """
    if interval.start is not None and interval.start > onset + RESOLUTION:
        return False
    initiations = [f.at for f in candidate.initiating if f.at is not None]
    if interval.start is not None:
        initiations.append(interval.start)
    if any(onset - window <= t <= onset + RESOLUTION for t in initiations):
        return True
    return (
        candidate.actor[0] in CHAOS_KINDS
        and interval.start is not None
        and interval.start <= onset + RESOLUTION
        and (interval.end is None or interval.end >= onset)
    )


def eligibility(
    candidates: Sequence[CandidateView],
    observations: Iterable[ChaosObservation],
    window: timedelta,
) -> Eligibility:
    """The §11 rule on competing ``SUPPORTED`` candidates, in their given order.

    If at least one candidate is displacing, linked and not ended before the onset, every candidate that ended
    before the onset is not eligible; otherwise nothing changes. Never leaves no candidate eligible.
    """
    keys = tuple(c.actor for c in candidates)
    onset = min((c.onset for c in candidates if c.onset is not None), default=None)
    if onset is None or len(candidates) < 2:
        return Eligibility(keys, ())
    intervals = chaos_intervals(observations)
    ended: dict[ActorKey, bool] = {}
    anchor = False
    for candidate in candidates:
        interval = effect_interval(candidate, intervals)
        ended[candidate.actor] = ended_before(interval, onset, window)
        if (
            not ended[candidate.actor]
            and candidate.linked
            and displacing(candidate, interval, onset, window)
        ):
            anchor = True
    if not anchor:
        return Eligibility(keys, ())
    return Eligibility(tuple(k for k in keys if not ended[k]), tuple(k for k in keys if ended[k]))
