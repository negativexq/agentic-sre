"""Which causal leader the operator is shown (roadmap C10, C12).

Presentation only: it never changes ``root_cause``, the ranking or the epistemic digest. The engine's
reported ``root_cause`` is chosen by score and, on a tie, by canonical name, which keeps replay
deterministic but is not causal evidence. What the operator sees is decided here instead, by epistemic
tier first (strong > supported > unestablished), considering only the highest tier present:

- one actor in that tier: shown alone (``SINGLE``), whatever ties it below;
- several actors in a supported or strong tier: shown together (``COMPETING``); in the supported tier only
  the candidates eligible by temporal relevance (m21 contract §11, ``W`` = 5 minutes): one whose observed
  effect ended before the onset yields to a linked candidate tied to the onset by an observation, and is listed
  as set aside instead (its claim is unchanged);
- an unestablished tier: its leader is shown only if it is the sole top score, has evidence in the
  incident window and, when its findings are timed, one of them at or after the onset less ``W`` (m21
  contract §18); otherwise ``NOT_ESTABLISHED`` with the tied candidates (``TIED_LEADERS``) or the leader as
  context (``NO_EVIDENCE_IN_INCIDENT_WINDOW``, ``NO_EVIDENCE_NEAR_ONSET``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from packages.rca.model import ClusterEvent, EntityRef
from packages.rca.temporal_relevance import (
    CandidateView,
    ChaosObservation,
    FindingView,
    eligibility,
)

# m21 contract §11.2: chosen by the pre-registered rule on the independent check; adopted for presentation only.
# §18 reuses it for an unestablished leader's evidence near the onset.
RELEVANCE_WINDOW = timedelta(minutes=5)


def near_onset(at: datetime | None, onset: datetime | None) -> bool | None:
    """Whether ``at`` is at or after ``onset`` less ``W`` (m21 contract §18); ``None`` when either is unknown."""
    if at is None or onset is None:
        return None
    return at >= onset - RELEVANCE_WINDOW


Display = Literal["SINGLE", "COMPETING", "NOT_ESTABLISHED"]
Tier = Literal["STRONG", "SUPPORTED", "UNESTABLISHED"]


@dataclass(frozen=True)
class LeadingActorProjection:
    display: Display
    tier: Tier | None
    candidates: tuple[EntityRef, ...]
    reason: str | None = None
    set_aside: tuple[EntityRef, ...] = ()  # supported candidates not eligible to lead (§11)


def _actors(hypotheses: Sequence[Any]) -> tuple[EntityRef, ...]:
    """Distinct actors in pool order (two claims on one actor are one candidate)."""
    return tuple(dict.fromkeys(h.causal_actor for h in hypotheses))


def leader_by_tier(
    pool: Sequence[Any], *, supported: Collection[str], strong: Collection[str]
) -> Any:
    """The engine's reported leader when the state is not ``RESOLVED``: the first hypothesis, in ranking
    order, of the highest epistemic tier present (m21 contract, leader selection by tier). Canonical name
    order remains only the ranking's last tie-break; it can no longer put a weaker tier ahead."""
    strong_members = [h for h in pool if h.hypothesis_id in strong]
    supported_members = [h for h in pool if h.hypothesis_id in supported]
    return (strong_members or supported_members or list(pool))[0]


def _view(hypothesis: Any) -> CandidateView:
    """What §11 reads of a claim; a claim without an onset (an older record) leaves the filter inactive."""
    actor = hypothesis.causal_actor
    initiating = getattr(hypothesis, "initiating_findings", ())
    return CandidateView(
        actor=(actor.kind, actor.name),
        onset=getattr(hypothesis, "episode_onset", None),
        initiating=tuple(FindingView(str(f.kind), f.at, f.summary) for f in initiating),
        linked=bool(getattr(hypothesis, "linked_symptoms", ()))
        or bool(getattr(hypothesis, "causal_paths", ())),
    )


def _relevant(
    members: Sequence[Any], events: Sequence[ClusterEvent]
) -> tuple[tuple[EntityRef, ...], tuple[EntityRef, ...]]:
    """(eligible, set aside) supported actors by §11, in pool order; every actor's first claim stands for it."""
    first: dict[EntityRef, Any] = {}
    for hypothesis in members:
        first.setdefault(hypothesis.causal_actor, hypothesis)
    result = eligibility(
        [_view(h) for h in first.values()],
        (
            ChaosObservation(e.entity.kind, e.entity.name, e.reason, e.first_at or e.last_at)
            for e in events
        ),
        RELEVANCE_WINDOW,
    )
    ineligible = set(result.ineligible)
    actors = tuple(first)
    return (
        tuple(a for a in actors if (a.kind, a.name) not in ineligible),
        tuple(a for a in actors if (a.kind, a.name) in ineligible),
    )


def project_leading_actor(
    pool: Sequence[Any],
    *,
    supported: Collection[str],
    strong: Collection[str],
    in_window: Callable[[datetime | None], bool | None],
    near_onset: Callable[[datetime | None], bool | None] = lambda _: None,
    events: Sequence[ClusterEvent] = (),
) -> LeadingActorProjection:
    """``pool``: the engine's selectable, not eliminated hypotheses in ranking order; ``near_onset``: whether
    an instant is at or after the onset less ``W`` (§18), ``None`` when unknowable; ``events``: the case's
    Kubernetes events, read only for the experiments' ``Applied`` and ``Recovered`` (§11)."""
    if not pool:
        return LeadingActorProjection("NOT_ESTABLISHED", None, (), "NO_CANDIDATE")
    for tier, ids in (("STRONG", strong), ("SUPPORTED", supported)):
        members = [h for h in pool if h.hypothesis_id in ids]
        if members:
            actors = _actors(members)
            set_aside: tuple[EntityRef, ...] = ()
            if tier == "SUPPORTED" and len(actors) > 1:
                actors, set_aside = _relevant(members, events)
            display: Display = "SINGLE" if len(actors) == 1 else "COMPETING"
            return LeadingActorProjection(display, tier, actors, set_aside=set_aside)  # type: ignore[arg-type]
    leader = pool[0]
    windows = [in_window(f.at) for f in leader.findings]
    if any(w is False for w in windows) and not any(w is True for w in windows):
        return LeadingActorProjection(
            "NOT_ESTABLISHED",
            "UNESTABLISHED",
            (leader.causal_actor,),
            "NO_EVIDENCE_IN_INCIDENT_WINDOW",
        )
    recent = [near_onset(f.at) for f in leader.findings]
    if any(r is False for r in recent) and not any(r is True for r in recent):
        return LeadingActorProjection(
            "NOT_ESTABLISHED", "UNESTABLISHED", (leader.causal_actor,), "NO_EVIDENCE_NEAR_ONSET"
        )
    top = max(h.score for h in pool)
    tied = _actors([h for h in pool if abs(h.score - top) < 1e-9])
    if len(tied) > 1:
        return LeadingActorProjection("NOT_ESTABLISHED", "UNESTABLISHED", tied, "TIED_LEADERS")
    return LeadingActorProjection("SINGLE", "UNESTABLISHED", (leader.causal_actor,))
