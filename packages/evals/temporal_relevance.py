"""Shadow of the temporal-relevance filter on supported leadership (m21 contract §11).

Measurement only: it reads a stored diagnosis document and the incident's Events and says which competing
`SUPPORTED` candidates would remain eligible to lead. The rule itself lives in ``packages.rca.temporal_relevance``
and is shared with the operator's leader projection; this module only builds its inputs from stored documents.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.temporal_relevance import (
    CandidateView,
    ChaosObservation,
    FindingView,
    eligibility,
)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def observations(events: Iterable[Mapping[str, Any]]) -> list[ChaosObservation]:
    """Stored Event bodies as the rule reads them."""
    out = []
    for event in events:
        involved = event.get("involvedObject") or {}
        at = (
            _time(event.get("eventTime"))
            or _time(event.get("firstTimestamp"))
            or _time(event.get("lastTimestamp"))
        )
        out.append(
            ChaosObservation(
                str(involved.get("kind") or ""),
                str(involved.get("name") or ""),
                str(event.get("reason") or ""),
                at,
            )
        )
    return out


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
    # the onset as the shadow was measured: the earliest of every stored hypothesis
    onset = min(
        (o for h in hypotheses.values() if (o := _time(h.get("episode_onset"))) is not None),
        default=None,
    )
    views = []
    for candidate in candidates:
        hypothesis = hypotheses.get(candidate, {})
        views.append(
            CandidateView(
                actor=candidate,
                onset=onset,
                initiating=tuple(
                    FindingView(
                        str(f.get("kind") or ""), _time(f.get("at")), str(f.get("summary") or "")
                    )
                    for f in hypothesis.get("initiating_findings") or []
                ),
                linked=bool(hypothesis.get("linked_symptoms"))
                or bool(hypothesis.get("causal_paths")),
            )
        )
    result = eligibility(views, observations(events), window)
    if not result.acted:
        return Shadow(display, tuple(candidates), (), False)
    return Shadow(
        "SINGLE" if len(result.eligible) == 1 else "COMPETING",
        result.eligible,
        result.ineligible,
        True,
    )


def shadow_all(
    documents: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], window: timedelta
) -> list[Shadow]:
    events = list(events)
    return [shadow_leadership(document, events, window) for document in documents]
