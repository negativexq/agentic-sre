"""Rule ``m16.ended-manifestation-episode.v1`` (contract ``m16.v1-a1``, section 17).

A Pod hypothesis that carries only its own failure manifestations, and whose
failure episode positively ended before the incident began, is not eligible
to be this incident's root cause. The end must be observed: a journal
tombstone recorded at or before onset, or a status observation taken after
onset plus grace that shows the Pod continuously Ready since before onset.
Missing data never ends an episode.

Every manifestation must name its exact Pod UID, and each UID's episode needs
its own end from evidence of that same UID; a same-named Pod with another UID
never ends it. A manifestation without a UID makes the rule inapplicable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from packages.rca.json_access import child
from packages.rca.model import (
    EliminationPrecondition,
    EntityRef,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
    PreconditionAuditReason,
    PreconditionResult,
    RequirementAuditReason,
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


class EpisodeEndDisqualificationReason(StrEnum):
    """Stable reasons why the exact-UID RECOVERED path cannot apply."""

    POST_ONSET_READY_FALSE = "POST_ONSET_READY_FALSE"
    RECOVERY_CONTINUITY_BROKEN = "RECOVERY_CONTINUITY_BROKEN"


@dataclass(frozen=True)
class InstanceEpisodeEnd:
    """The positively observed end of one exact Pod instance's episode."""

    uid: str
    target: str
    basis: EpisodeEndBasis
    ended_at: datetime
    observed_at: datetime
    last_manifestation_at: datetime
    end_evidence_id: str
    decisive_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EndedEpisode:
    """A passed rule evaluation, with everything its audit record needs.

    ``instances`` holds one end per exact Pod UID the manifestations name, in
    UID order. The single-value accessors are defined only for one instance.
    """

    hypothesis_id: str
    actor: EntityRef
    onset: datetime
    boundary: datetime
    instances: tuple[InstanceEpisodeEnd, ...]
    manifestation_evidence_ids: tuple[str, ...]
    preconditions: tuple[EliminationPrecondition, ...]

    def _only(self) -> InstanceEpisodeEnd:
        if len(self.instances) != 1:
            raise ValueError(f"{len(self.instances)} instances; read ``instances`` instead")
        return self.instances[0]

    @property
    def basis(self) -> EpisodeEndBasis:
        return self._only().basis

    @property
    def ended_at(self) -> datetime:
        return self._only().ended_at

    @property
    def observed_at(self) -> datetime:
        return self._only().observed_at

    @property
    def last_manifestation_at(self) -> datetime:
        return self._only().last_manifestation_at

    @property
    def end_evidence_id(self) -> str:
        return self._only().end_evidence_id


@dataclass(frozen=True)
class EpisodeEndPreconditionEvaluation:
    """Non-decision audit outcomes for one in-scope A1 hypothesis."""

    results: tuple[PreconditionResult, ...] = ()
    reasons: tuple[PreconditionAuditReason, ...] = ()


@dataclass(frozen=True)
class InstanceRequirement:
    """Requirement-level outcome for one exact Pod instance (M19-5.5).

    Answers only "has the evidence expected about this UID matured?". A PASS
    here does not imply an A1 elimination: another UID may still be pending,
    or the rule may reject the ended episode for another reason.
    """

    uid: str
    result: PreconditionResult | None = None
    audit_reason: RequirementAuditReason | None = None


@dataclass(frozen=True)
class EpisodeEndEvaluations:
    """A1 positive ends plus neutral/pending precondition audit outcomes."""

    ended_episodes: Mapping[str, EndedEpisode]
    preconditions: Mapping[str, tuple[PreconditionResult, ...]]
    reasons: Mapping[str, tuple[PreconditionAuditReason, ...]]
    # Per-UID requirement metadata; no decision reads it.
    instance_requirements: Mapping[str, tuple[InstanceRequirement, ...]] = field(
        default_factory=dict
    )


def _version_uid(version: ObjectVersion) -> str | None:
    """The UID the version itself records; never looked up by name."""
    if version.uid:
        return version.uid
    uid = child(version.body, "metadata").get("uid")
    return uid if isinstance(uid, str) and uid else None


def _terminated(
    uid: str,
    versions: Sequence[ObjectVersion],
    statuses: Sequence[PodStatusObservation],
    onset: datetime,
) -> tuple[datetime, datetime, str, tuple[str, ...]] | None:
    """(end, observed_at, evidence, decisive) when this exact instance was deleted by onset.

    ``decisive`` is every qualifying deletion record of the instance: the
    trailing real tombstones at or before onset.
    """
    own = [version for version in versions if _version_uid(version) == uid]
    if not own:
        return None
    last = own[-1]
    if last.lifecycle is not Lifecycle.DELETED or last.evidence_id in _SYNTHETIC_TOMBSTONES:
        return None
    if last.observed_at > onset:
        return None
    # A later sighting of the same instance, or one whose identity is unknown,
    # means deletion is not proven; another UID under the same name is not it.
    if any(
        _version_uid(version) in (uid, None) and version.observed_at > last.observed_at
        for version in versions
    ):
        return None
    if any(
        status.uid in (uid, None) and status.observed_at > last.observed_at for status in statuses
    ):
        return None
    decisive: list[str] = []
    for version in reversed(own):
        if (
            version.lifecycle is not Lifecycle.DELETED
            or version.evidence_id in _SYNTHETIC_TOMBSTONES
            or version.observed_at > onset
        ):
            break
        decisive.append(version.evidence_id)
    return last.observed_at, last.observed_at, last.evidence_id, tuple(reversed(decisive))


def _recovered(
    uid: str,
    statuses: Sequence[PodStatusObservation],
    onset: datetime,
    boundary: datetime,
) -> tuple[datetime, datetime, str, tuple[str, ...]] | None:
    """(ready_since, observed_at, evidence, decisive) for readiness across onset.

    ``decisive`` is every covering observation of the instance, not only the
    latest one; continuity makes them all agree on ``ready_since``.
    """
    own = [status for status in statuses if status.uid == uid]
    covering = [
        status
        for status in own
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
    for status in own:
        if since <= status.observed_at <= latest.observed_at:
            if status.ready is not True or status.ready_since != since:
                return None
    decisive = tuple(
        status.evidence_id for status in sorted(covering, key=lambda status: status.observed_at)
    )
    return since, latest.observed_at, latest.evidence_id, decisive


def _manifestation_groups(
    hypothesis: Hypothesis, onset: datetime | None
) -> tuple[EntityRef, dict[str, list[datetime]]] | None:
    """Return exact-UID manifestations only when frozen A1 scope applies."""
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
        or any(finding.at is None for finding in findings)
    ):
        return None
    groups: dict[str, list[datetime]] = {}
    for finding in findings:
        if finding.entity_instance is None or finding.at is None:
            return None
        groups.setdefault(finding.entity_instance.uid, []).append(finding.at)
    return actor, groups


def _scope_exit_uids(hypothesis: Hypothesis, onset: datetime | None) -> tuple[str, ...]:
    """UIDs of a present hypothesis whose frozen A1 scope now positively fails.

    Only structural prerequisites count: initiating premises, or findings that
    are not manifestations of the actor. A missing onset, UID or timestamp is
    missing evidence, never a scope exit.
    """
    actor = hypothesis.causal_actor
    findings = hypothesis.findings
    if onset is None or actor.kind != "Pod" or not findings:
        return ()
    if not (
        hypothesis.initiating_findings
        or episode_source_capable_initiating_findings(hypothesis)
        or any(
            finding.kind not in _MANIFESTATION_KINDS or finding.entity != actor
            for finding in findings
        )
    ):
        return ()
    return tuple(
        sorted(
            {
                finding.entity_instance.uid
                for finding in findings
                if finding.kind in _MANIFESTATION_KINDS
                and finding.entity == actor
                and finding.entity_instance is not None
            }
        )
    )


def _requirement(uid: str, outcome: EpisodeEndPreconditionEvaluation) -> InstanceRequirement | None:
    if outcome.results:
        (result,) = outcome.results
        return InstanceRequirement(uid, result=result)
    if outcome.reasons:
        (reason,) = outcome.reasons
        return InstanceRequirement(uid, audit_reason=RequirementAuditReason(reason.value))
    return None


def _recovery_precondition(
    uid: str,
    statuses: Sequence[PodStatusObservation],
    *,
    onset: datetime,
    boundary: datetime,
    evaluation_at: datetime | None,
    last_manifestation_at: datetime,
) -> EpisodeEndPreconditionEvaluation:
    """Classify only positive exact-UID facts; absence remains neutral."""
    own = tuple(status for status in statuses if status.uid == uid)

    # A real post-onset false observation is positive evidence against this
    # recovery path. Equality is included because the continuity interval starts
    # at onset and the existing A1 interval checks are inclusive.
    if any(status.ready is False and status.observed_at >= onset for status in own):
        return EpisodeEndPreconditionEvaluation(
            results=(
                PreconditionResult.disqualified(
                    EpisodeEndDisqualificationReason.POST_ONSET_READY_FALSE.value
                ),
            )
        )

    # Ready=True with a last transition after onset positively establishes that
    # this readiness interval did not cover onset. `ready_since` is the source
    # ledger's lastTransitionTime, not an inferred timestamp.
    if any(
        status.ready is True
        and status.ready_since is not None
        and status.ready_since > onset
        and status.observed_at >= onset
        for status in own
    ):
        return EpisodeEndPreconditionEvaluation(
            results=(
                PreconditionResult.disqualified(
                    EpisodeEndDisqualificationReason.RECOVERY_CONTINUITY_BROKEN.value
                ),
            )
        )

    candidates = tuple(
        status
        for status in own
        if onset <= status.observed_at < boundary
        and status.ready is True
        and status.ready_since is not None
        and status.ready_since <= onset
    )
    if candidates:
        candidate = max(candidates, key=lambda status: status.observed_at)
        since = candidate.ready_since
        assert since is not None
        # Match the existing continuity predicate over the known portion of
        # [ready_since, latest observation]. A positive conflicting observation
        # disqualifies; an unknown/missing status never does.
        broken = any(
            status.observed_at >= since
            and status.observed_at <= candidate.observed_at
            and (
                status.ready is False
                or (
                    status.ready is True
                    and status.ready_since is not None
                    and status.ready_since != since
                )
            )
            for status in own
        )
        if broken:
            return EpisodeEndPreconditionEvaluation(
                results=(
                    PreconditionResult.disqualified(
                        EpisodeEndDisqualificationReason.RECOVERY_CONTINUITY_BROKEN.value
                    ),
                )
            )
        if last_manifestation_at < since:
            if evaluation_at is not None and evaluation_at >= boundary:
                return EpisodeEndPreconditionEvaluation(
                    reasons=(PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,)
                )
            return EpisodeEndPreconditionEvaluation(results=(PreconditionResult.pending(boundary),))
        return EpisodeEndPreconditionEvaluation()

    if evaluation_at is not None and evaluation_at >= boundary:
        reason = (
            PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE
            if any(status.observed_at >= onset for status in own)
            else PreconditionAuditReason.NO_DATA_AFTER_DEADLINE
        )
        return EpisodeEndPreconditionEvaluation(reasons=(reason,))
    return EpisodeEndPreconditionEvaluation()


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
    if any(finding.at is None for finding in findings):
        return None
    # instance_bound: every manifestation must name its exact Pod UID.
    groups: dict[str, list[datetime]] = {}
    for finding in findings:
        if finding.entity_instance is None or finding.at is None:
            return None
        groups.setdefault(finding.entity_instance.uid, []).append(finding.at)
    boundary = onset + grace
    versions = history.get(actor, ())
    statuses = tuple(status for status in pod_statuses if status.pod == actor)
    instances: list[InstanceEpisodeEnd] = []
    for uid in sorted(groups):
        basis = EpisodeEndBasis.TERMINATED
        end = _terminated(uid, versions, statuses, onset)
        if end is None:
            basis = EpisodeEndBasis.RECOVERED
            end = _recovered(uid, statuses, onset, boundary)
        if end is None:
            return None
        ended_at, observed_at, evidence_id, decisive = end
        last_manifestation = max(groups[uid])
        if not last_manifestation < ended_at:
            return None
        instances.append(
            InstanceEpisodeEnd(
                uid=uid,
                target=f"{actor.canonical}@{uid}",
                basis=basis,
                ended_at=ended_at,
                observed_at=observed_at,
                last_manifestation_at=last_manifestation,
                end_evidence_id=evidence_id,
                decisive_evidence_ids=decisive,
            )
        )
    manifestation_ids = tuple(
        dict.fromkeys(evidence for finding in findings for evidence in finding.evidence_ids)
    )
    targets = ", ".join(item.target for item in instances)
    end_checks = tuple(
        EliminationPrecondition(
            name=f"positive_episode_end_{basis.value.lower()}",
            passed=True,
            detail="; ".join(_end_detail(item, onset) for item in instances if item.basis is basis),
        )
        for basis in sorted({item.basis for item in instances})
    )
    return EndedEpisode(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=actor,
        onset=onset,
        boundary=boundary,
        instances=tuple(instances),
        manifestation_evidence_ids=manifestation_ids,
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
                name="instance_bound",
                passed=True,
                detail=f"every manifestation names its exact Pod UID: {targets}",
            ),
            EliminationPrecondition(
                name="per_instance_end",
                passed=True,
                detail=f"{len(instances)} instance(s), each with its own observed end",
            ),
            *end_checks,
            EliminationPrecondition(
                name="no_overlap",
                passed=True,
                detail="; ".join(
                    f"{item.target}: last manifestation "
                    f"{item.last_manifestation_at.isoformat()} < episode end "
                    f"{item.ended_at.isoformat()}"
                    for item in instances
                ),
            ),
        ),
    )


def _end_detail(item: InstanceEpisodeEnd, onset: datetime) -> str:
    if item.basis is EpisodeEndBasis.TERMINATED:
        return (
            f"{item.target}: journal tombstone recorded at {item.observed_at.isoformat()} "
            f"<= onset {onset.isoformat()}"
        )
    return (
        f"{item.target}: Ready since {item.ended_at.isoformat()} <= onset, observed at "
        f"{item.observed_at.isoformat()} >= onset + grace"
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


def evaluate_ended_episodes(
    hypotheses: Sequence[Hypothesis],
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    pod_statuses: Sequence[PodStatusObservation],
    onset: datetime | None,
    grace: timedelta,
    evaluation_at: datetime | None,
) -> EpisodeEndEvaluations:
    """Evaluate A1 ends and retain tri-state outcomes without decision effects.

    ``evaluation_at`` is the frozen observation cutoff. At or after the
    maturity boundary, missing/partial status is recorded as a neutral audit
    reason rather than a perpetually pending requirement.
    """
    ended: dict[str, EndedEpisode] = {}
    preconditions: dict[str, list[PreconditionResult]] = {}
    reasons: dict[str, list[PreconditionAuditReason]] = {}
    requirements: dict[str, list[InstanceRequirement]] = {}
    for hypothesis in hypotheses:
        positive = assess_ended_episode(
            hypothesis,
            history=history,
            pod_statuses=pod_statuses,
            onset=onset,
            grace=grace,
        )
        if positive is not None:
            ended[hypothesis.hypothesis_id] = positive
            requirements[hypothesis.hypothesis_id] = [
                InstanceRequirement(instance.uid, result=PreconditionResult.passed())
                for instance in positive.instances
            ]
            continue
        scoped = _manifestation_groups(hypothesis, onset)
        if scoped is None or onset is None:
            exited = _scope_exit_uids(hypothesis, onset)
            if exited:
                requirements[hypothesis.hypothesis_id] = [
                    InstanceRequirement(uid, audit_reason=RequirementAuditReason.SCOPE_EXITED)
                    for uid in exited
                ]
            continue
        actor, groups = scoped
        statuses = tuple(status for status in pod_statuses if status.pod == actor)
        versions = history.get(actor, ())
        for uid, manifestation_times in sorted(groups.items()):
            # Preserve termination and a mature recovery attempt as-is. The
            # existing assessor may still reject them for no-overlap, but that
            # does not turn them into a pending recovery.
            # The UID's own evidence obligation matured either way, so its
            # requirement passes even when the hypothesis-level end does not.
            boundary = onset + grace
            if (
                _terminated(uid, versions, statuses, onset) is not None
                or _recovered(uid, statuses, onset, boundary) is not None
            ):
                requirements.setdefault(hypothesis.hypothesis_id, []).append(
                    InstanceRequirement(uid, result=PreconditionResult.passed())
                )
                continue
            outcome = _recovery_precondition(
                uid,
                statuses,
                onset=onset,
                boundary=boundary,
                evaluation_at=evaluation_at,
                last_manifestation_at=max(manifestation_times),
            )
            if outcome.results:
                preconditions.setdefault(hypothesis.hypothesis_id, []).extend(outcome.results)
            if outcome.reasons:
                reasons.setdefault(hypothesis.hypothesis_id, []).extend(outcome.reasons)
            requirement = _requirement(uid, outcome)
            if requirement is not None:
                requirements.setdefault(hypothesis.hypothesis_id, []).append(requirement)
    return EpisodeEndEvaluations(
        ended_episodes=ended,
        preconditions={key: tuple(value) for key, value in preconditions.items()},
        reasons={key: tuple(value) for key, value in reasons.items()},
        instance_requirements={key: tuple(value) for key, value in requirements.items()},
    )


__all__ = [
    "RULE_ID",
    "RULE_VERSION",
    "EndedEpisode",
    "EpisodeEndBasis",
    "EpisodeEndDisqualificationReason",
    "EpisodeEndPreconditionEvaluation",
    "EpisodeEndEvaluations",
    "InstanceEpisodeEnd",
    "InstanceRequirement",
    "assess_ended_episode",
    "assess_ended_episodes",
    "evaluate_ended_episodes",
]
