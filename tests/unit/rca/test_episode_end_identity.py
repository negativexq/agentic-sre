"""A1 identity matrix: exact Pod UIDs bind episodes to their own ends (M19-1.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.episode_end import EpisodeEndBasis, assess_ended_episode
from packages.rca.model import (
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
)
from packages.rca.resolution import _ended_episode_elimination

ONSET = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
POD = EntityRef(namespace="shop", kind="Pod", name="web-0")


def _minutes(value: float) -> datetime:
    return ONSET + timedelta(minutes=value)


def _unhealthy(uid: str | None, minutes: float = -10) -> Finding:
    return Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        entity_instance=EntityInstanceRef(entity=POD, uid=uid) if uid else None,
        at=_minutes(minutes),
        incident_onset=ONSET,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="Unhealthy x2: readiness probe failed",
        evidence_ids=(f"event:{uid}:{minutes}",),
    )


def _hypothesis(*findings: Finding) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hypothesis:web-0",
        causal_actor=POD,
        members=(POD,),
        findings=findings,
        supporting_findings=findings,
        causal_explanation="DIRECT",
    )


def _status(
    uid: str, observed: float, *, ready: bool = True, since: float | None = -5
) -> PodStatusObservation:
    return PodStatusObservation(
        pod=POD,
        uid=uid,
        observed_at=_minutes(observed),
        ready=ready,
        ready_since=_minutes(since) if since is not None else None,
        evidence_id=f"status:{uid}:{observed}",
    )


def _version(
    uid: str, minutes: float, lifecycle: Lifecycle, evidence: str | None = None
) -> ObjectVersion:
    return ObjectVersion(
        entity=POD,
        uid=uid,
        observed_at=_minutes(minutes),
        body={"kind": "Pod", "metadata": {"name": POD.name, "namespace": "shop", "uid": uid}},
        evidence_id=evidence or f"journal:{uid}:{minutes}",
        lifecycle=lifecycle,
    )


def _assess(
    hypothesis: Hypothesis,
    *,
    versions: tuple[ObjectVersion, ...] = (),
    statuses: tuple[PodStatusObservation, ...] = (),
) -> Any:
    return assess_ended_episode(
        hypothesis,
        history={POD: list(versions)} if versions else {},
        pod_statuses=statuses,
        onset=ONSET,
        grace=GRACE,
    )


def test_event_of_a_with_readiness_of_b_is_not_recovered() -> None:
    assert _assess(_hypothesis(_unhealthy("A")), statuses=(_status("B", 20),)) is None


def test_event_of_a_with_deletion_of_a_is_terminated() -> None:
    versions = (_version("A", -30, Lifecycle.CREATED), _version("A", -2, Lifecycle.DELETED))
    ended = _assess(_hypothesis(_unhealthy("A")), versions=versions)

    assert ended is not None
    assert ended.basis is EpisodeEndBasis.TERMINATED
    assert ended.end_evidence_id == "journal:A:-2"


def test_event_of_a_with_deletion_of_b_is_not_terminated() -> None:
    versions = (_version("B", -30, Lifecycle.CREATED), _version("B", -2, Lifecycle.DELETED))
    assert _assess(_hypothesis(_unhealthy("A")), versions=versions) is None


def test_two_uids_with_only_one_end_are_not_eliminated() -> None:
    hypothesis = _hypothesis(_unhealthy("A"), _unhealthy("B", -8))
    versions = (_version("A", -30, Lifecycle.CREATED), _version("A", -2, Lifecycle.DELETED))
    assert _assess(hypothesis, versions=versions) is None


def test_two_uids_each_with_its_own_end_are_eliminated_with_two_targets() -> None:
    # A failed at -15 and was deleted at -12; B failed at -8 and recovered.
    hypothesis = _hypothesis(_unhealthy("A", -15), _unhealthy("B", -8))
    versions = (_version("A", -30, Lifecycle.CREATED), _version("A", -12, Lifecycle.DELETED))
    ended = _assess(hypothesis, versions=versions, statuses=(_status("B", 20),))

    assert ended is not None
    assert [(item.uid, item.basis) for item in ended.instances] == [
        ("A", EpisodeEndBasis.TERMINATED),
        ("B", EpisodeEndBasis.RECOVERED),
    ]
    item = _ended_episode_elimination(ended)
    assert [basis.target for basis in item.time_basis] == [
        "shop/Pod/web-0@A",
        "shop/Pod/web-0@B",
    ]


def test_manifestation_without_uid_is_inapplicable() -> None:
    hypothesis = _hypothesis(_unhealthy("A"), _unhealthy(None, -9))
    versions = (_version("A", -30, Lifecycle.CREATED), _version("A", -2, Lifecycle.DELETED))
    # instance_bound fails: the rule does not apply, so nothing is eliminated.
    assert _assess(hypothesis, versions=versions, statuses=(_status("A", 20),)) is None


def test_statefulset_reincarnation_never_eliminates_the_new_instance() -> None:
    # web-0@A was deleted; web-0@B took its name and was Unhealthy and NotReady.
    versions = (
        _version("A", -40, Lifecycle.CREATED),
        _version("A", -20, Lifecycle.DELETED),
        _version("B", -19, Lifecycle.CREATED),
    )
    statuses = (_status("B", 20, ready=False, since=None),)
    assert _assess(_hypothesis(_unhealthy("B")), versions=versions, statuses=statuses) is None


def test_synthetic_missing_tombstone_is_never_terminated() -> None:
    versions = (
        _version("A", -30, Lifecycle.CREATED),
        _version("A", -2, Lifecycle.DELETED, evidence="cluster:missing"),
    )
    assert _assess(_hypothesis(_unhealthy("A")), versions=versions) is None


def test_recovered_decisive_evidence_holds_every_qualifying_observation_of_the_uid() -> None:
    statuses = (
        _status("A", 16),
        _status("A", 20),
        _status("A", 25),
        _status("A", 10),  # before onset + grace: does not qualify
        _status("B", 22),  # another instance under the same name
    )
    ended = _assess(_hypothesis(_unhealthy("A")), statuses=statuses)
    item = _ended_episode_elimination(ended)

    assert item.decisive_evidence_ids == ("status:A:16", "status:A:20", "status:A:25")
