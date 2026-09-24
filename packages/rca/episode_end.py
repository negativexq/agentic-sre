"""Rule ``m16.ended-manifestation-episode.v1`` (contract ``m16.v1-a1``, section 17).

A Pod hypothesis that carries only its own failure manifestations, and whose
failure episode positively ended before the incident began, is not eligible
to be this incident's root cause. The end must be observed: a journal
tombstone recorded at or before onset, or a status observation taken after
onset plus grace that shows the Pod continuously Ready since before onset.
Missing data never ends an episode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from packages.rca.model import (
    EliminationPrecondition,
    EntityRef,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
)
from packages.rca.root_cause_eligibility import episode_source_capable_initiating_findings

RULE_ID = "m16.ended-manifestation-episode"
RULE_VERSION = "v1"
_MANIFESTATION_KINDS = frozenset({FindingKind.FAILURE_EVENT, FindingKind.CONTAINER_FAILURE})
# Tombstones synthesized from absence in a listing carry the diagnosis time and
# prove nothing about when the Pod went away.
_SYNTHETIC_TOMBSTONES = frozenset({"cluster:missing"})


class EpisodeEndBasis(StrEnum):
    TERMINATED = "TERMINATED"
    RECOVERED = "RECOVERED"


@dataclass(frozen=True)
class EndedEpisode:
    """A passed rule evaluation, with everything its audit record needs."""

    hypothesis_id: str
    actor: EntityRef
    basis: EpisodeEndBasis
    onset: datetime
    boundary: datetime
    ended_at: datetime
    observed_at: datetime
    last_manifestation_at: datetime
    manifestation_evidence_ids: tuple[str, ...]
    end_evidence_id: str
    preconditions: tuple[EliminationPrecondition, ...]


def _terminated(
    versions: Sequence[ObjectVersion],
    statuses: Sequence[PodStatusObservation],
    onset: datetime,
) -> tuple[datetime, datetime, str] | None:
    """(end, observed_at, evidence) when the Pod was positively deleted by onset."""
    if not versions:
        return None
    last = versions[-1]
    if last.lifecycle is not Lifecycle.DELETED or last.evidence_id in _SYNTHETIC_TOMBSTONES:
        return None
    if last.observed_at > onset:
        return None
    # A later sighting means a same-named Pod existed again after the tombstone.
    if any(status.observed_at > last.observed_at for status in statuses):
        return None
    return last.observed_at, last.observed_at, last.evidence_id


def _recovered(
    statuses: Sequence[PodStatusObservation],
    onset: datetime,
    boundary: datetime,
) -> tuple[datetime, datetime, str] | None:
    """(ready_since, observed_at, evidence) for continuous readiness across onset."""
    covering = [
        status
        for status in statuses
        if status.observed_at >= boundary
        and status.ready is True
        and status.ready_since is not None
        and status.ready_since <= onset
    ]
    if not covering:
        return None
    latest = max(covering, key=lambda status: status.observed_at)
    since = latest.ready_since
    assert since is not None
    # Every observation of the same Pod instance inside [since, latest] must
    # agree; a disagreeing one means the continuity claim is not safe.
    for status in statuses:
        if since <= status.observed_at <= latest.observed_at and status.uid == latest.uid:
            if status.ready is not True or status.ready_since != since:
                return None
    return since, latest.observed_at, latest.evidence_id


def assess_ended_episode(
    hypothesis: Hypothesis,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    pod_statuses: Sequence[PodStatusObservation],
    onset: datetime | None,
    grace: timedelta,
) -> EndedEpisode | None:
    """Return the ended episode only when every precondition passes."""
    actor = hypothesis.causal_actor
    if onset is None or actor.kind != "Pod":
        return None
    findings = hypothesis.findings
    if (
        not findings
        or hypothesis.initiating_findings
        or episode_source_capable_initiating_findings(hypothesis)
        or any(
            finding.kind not in _MANIFESTATION_KINDS or finding.entity != actor
            for finding in findings
        )
    ):
        return None
    times = [finding.at for finding in findings]
    if any(at is None for at in times):
        return None
    last_manifestation = max(at for at in times if at is not None)
    boundary = onset + grace
    statuses = tuple(status for status in pod_statuses if status.pod == actor)
    end = _terminated(history.get(actor, ()), statuses, onset)
    basis = EpisodeEndBasis.TERMINATED
    if end is None:
        end = _recovered(statuses, onset, boundary)
        basis = EpisodeEndBasis.RECOVERED
    if end is None:
        return None
    ended_at, observed_at, evidence_id = end
    if not last_manifestation < ended_at:
        return None
    manifestation_ids = tuple(
        dict.fromkeys(evidence for finding in findings for evidence in finding.evidence_ids)
    )
    end_detail = (
        f"journal tombstone recorded at {observed_at.isoformat()} <= onset {onset.isoformat()}"
        if basis is EpisodeEndBasis.TERMINATED
        else (
            f"Ready since {ended_at.isoformat()} <= onset, observed at "
            f"{observed_at.isoformat()} >= onset + grace"
        )
    )
    return EndedEpisode(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=actor,
        basis=basis,
        onset=onset,
        boundary=boundary,
        ended_at=ended_at,
        observed_at=observed_at,
        last_manifestation_at=last_manifestation,
        manifestation_evidence_ids=manifestation_ids,
        end_evidence_id=evidence_id,
        preconditions=(
            EliminationPrecondition(name="pod_actor", passed=True, detail=actor.canonical),
            EliminationPrecondition(
                name="manifestation_only",
                passed=True,
                detail=(
                    f"{len(findings)} FAILURE_EVENT/CONTAINER_FAILURE finding(s) on the actor; "
                    "no initiating evidence"
                ),
            ),
            EliminationPrecondition(
                name="timed_manifestations", passed=True, detail="every finding is timestamped"
            ),
            EliminationPrecondition(
                name=f"positive_episode_end_{basis.value.lower()}",
                passed=True,
                detail=end_detail,
            ),
            EliminationPrecondition(
                name="no_overlap",
                passed=True,
                detail=(
                    f"last manifestation {last_manifestation.isoformat()} < episode end "
                    f"{ended_at.isoformat()}"
                ),
            ),
        ),
    )


def assess_ended_episodes(
    hypotheses: Sequence[Hypothesis],
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    pod_statuses: Sequence[PodStatusObservation],
    onset: datetime | None,
    grace: timedelta,
) -> dict[str, EndedEpisode]:
    ended: dict[str, EndedEpisode] = {}
    for hypothesis in hypotheses:
        item = assess_ended_episode(
            hypothesis, history=history, pod_statuses=pod_statuses, onset=onset, grace=grace
        )
        if item is not None:
            ended[hypothesis.hypothesis_id] = item
    return ended


__all__ = [
    "RULE_ID",
    "RULE_VERSION",
    "EndedEpisode",
    "EpisodeEndBasis",
    "assess_ended_episode",
    "assess_ended_episodes",
]
