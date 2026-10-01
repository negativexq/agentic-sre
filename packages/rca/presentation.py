"""Which causal leader the operator is shown (roadmap C10, C12).

Presentation only: it never changes ``root_cause``, the ranking or the epistemic digest. The engine's
reported ``root_cause`` is chosen by score and, on a tie, by canonical name, which keeps replay
deterministic but is not causal evidence. What the operator sees is decided here instead, by epistemic
tier first (strong > supported > unestablished), considering only the highest tier present:

- one actor in that tier: shown alone (``SINGLE``), whatever ties it below;
- several actors in a supported or strong tier: shown together (``COMPETING``);
- an unestablished tier: its leader is shown only if it is the sole top score and has evidence in the
  incident window; otherwise ``NOT_ESTABLISHED`` with the tied candidates (``TIED_LEADERS``) or the
  leader as context (``NO_EVIDENCE_IN_INCIDENT_WINDOW``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from packages.rca.model import EntityRef

Display = Literal["SINGLE", "COMPETING", "NOT_ESTABLISHED"]
Tier = Literal["STRONG", "SUPPORTED", "UNESTABLISHED"]


@dataclass(frozen=True)
class LeadingActorProjection:
    display: Display
    tier: Tier | None
    candidates: tuple[EntityRef, ...]
    reason: str | None = None


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


def project_leading_actor(
    pool: Sequence[Any],
    *,
    supported: Collection[str],
    strong: Collection[str],
    in_window: Callable[[datetime | None], bool | None],
) -> LeadingActorProjection:
    """``pool``: the engine's selectable, not eliminated hypotheses in ranking order."""
    if not pool:
        return LeadingActorProjection("NOT_ESTABLISHED", None, (), "NO_CANDIDATE")
    for tier, ids in (("STRONG", strong), ("SUPPORTED", supported)):
        members = [h for h in pool if h.hypothesis_id in ids]
        if members:
            actors = _actors(members)
            display: Display = "SINGLE" if len(actors) == 1 else "COMPETING"
            return LeadingActorProjection(display, tier, actors)  # type: ignore[arg-type]
    leader = pool[0]
    windows = [in_window(f.at) for f in leader.findings]
    if any(w is False for w in windows) and not any(w is True for w in windows):
        return LeadingActorProjection(
            "NOT_ESTABLISHED",
            "UNESTABLISHED",
            (leader.causal_actor,),
            "NO_EVIDENCE_IN_INCIDENT_WINDOW",
        )
    top = max(h.score for h in pool)
    tied = _actors([h for h in pool if abs(h.score - top) < 1e-9])
    if len(tied) > 1:
        return LeadingActorProjection("NOT_ESTABLISHED", "UNESTABLISHED", tied, "TIED_LEADERS")
    return LeadingActorProjection("SINGLE", "UNESTABLISHED", (leader.causal_actor,))
