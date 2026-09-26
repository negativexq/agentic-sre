"""A1 binds each manifestation episode to its exact Pod UID (M19-1.6)."""

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


def _hypothesis(uid: str | None) -> Hypothesis:
    finding = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        entity_instance=EntityInstanceRef(entity=POD, uid=uid) if uid else None,
        at=ONSET - timedelta(minutes=10),
        incident_onset=ONSET,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="Unhealthy x2",
        evidence_ids=("event-1",),
    )
    return Hypothesis(
        hypothesis_id="hypothesis:web",
        causal_actor=POD,
        members=(POD,),
        findings=(finding,),
        supporting_findings=(finding,),
        causal_explanation="DIRECT",
    )


def _ready(uid: str) -> PodStatusObservation:
    return PodStatusObservation(
        pod=POD,
        uid=uid,
        observed_at=ONSET + timedelta(minutes=20),
        ready=True,
        ready_since=ONSET - timedelta(minutes=5),
        evidence_id=f"status:{uid}",
    )


def _deleted(uid: str) -> dict[EntityRef, list[ObjectVersion]]:
    body: dict[str, Any] = {"kind": "Pod", "metadata": {"name": POD.name, "uid": uid}}
    return {
        POD: [
            ObjectVersion(
                entity=POD,
                uid=uid,
                observed_at=ONSET - timedelta(minutes=2),
                body=body,
                evidence_id=f"journal:{uid}",
                lifecycle=Lifecycle.DELETED,
            )
        ]
    }


def _assess(hypothesis: Hypothesis, **kwargs: Any) -> Any:
    return assess_ended_episode(
        hypothesis,
        history=kwargs.get("history", {}),
        pod_statuses=kwargs.get("statuses", ()),
        onset=ONSET,
        grace=GRACE,
    )


def test_manifestation_without_uid_makes_the_rule_inapplicable() -> None:
    assert _assess(_hypothesis(None), statuses=(_ready("a"),)) is None
    assert _assess(_hypothesis(None), history=_deleted("a")) is None


def test_readiness_of_another_uid_never_recovers_the_episode() -> None:
    assert _assess(_hypothesis("a"), statuses=(_ready("b"),)) is None
    assert _assess(_hypothesis("a"), statuses=(_ready("a"),)).basis is EpisodeEndBasis.RECOVERED


def test_deletion_of_another_uid_never_terminates_the_episode() -> None:
    assert _assess(_hypothesis("a"), history=_deleted("b")) is None
    assert _assess(_hypothesis("a"), history=_deleted("a")).basis is EpisodeEndBasis.TERMINATED


def test_audit_time_basis_names_the_exact_instance() -> None:
    ended = _assess(_hypothesis("a"), history=_deleted("a"))
    item = _ended_episode_elimination(ended)

    (basis,) = item.time_basis
    assert basis.target == "shop/Pod/web-0@a"
    assert basis.evidence_ids == ("journal:a",)
    assert basis.certainty == "TERMINATED"
    assert [check.name for check in item.preconditions][3:5] == [
        "instance_bound",
        "per_instance_end",
    ]
