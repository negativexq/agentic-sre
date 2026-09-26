"""Eliminations name every record that satisfied their deciding precondition (M19-1.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.episode_end import assess_ended_episode
from packages.rca.model import (
    EliminationPrecondition,
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
    ResolutionElimination,
)
from packages.rca.resolution import _ended_episode_elimination, _mechanism_elimination
from packages.rca.resource_mechanism import MechanismMismatch, PodCoverage

ONSET = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
POD = EntityRef(namespace="shop", kind="Pod", name="worker-abc")


def _minutes(value: float) -> datetime:
    return ONSET + timedelta(minutes=value)


def _stale() -> Hypothesis:
    finding = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        entity_instance=EntityInstanceRef(entity=POD, uid="u1"),
        at=_minutes(-10),
        incident_onset=ONSET,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="Unhealthy x3",
        evidence_ids=("event-1",),
    )
    return Hypothesis(
        hypothesis_id="hypothesis:worker",
        causal_actor=POD,
        members=(POD,),
        findings=(finding,),
        supporting_findings=(finding,),
        causal_explanation="DIRECT",
    )


def _status(observed: float) -> PodStatusObservation:
    return PodStatusObservation(
        pod=POD,
        uid="u1",
        observed_at=_minutes(observed),
        ready=True,
        ready_since=_minutes(-5),
        evidence_id=f"status:{observed}",
    )


def _assess(**kwargs: Any) -> Any:
    return assess_ended_episode(
        _stale(),
        history=kwargs.get("history", {}),
        pod_statuses=kwargs.get("statuses", ()),
        onset=ONSET,
        grace=GRACE,
    )


def test_recovered_records_every_qualifying_observation() -> None:
    # 10 is before onset + grace, so it does not qualify; 16, 20 and 25 do.
    statuses = (_status(25), _status(10), _status(16), _status(20))
    item = _ended_episode_elimination(_assess(statuses=statuses))

    assert item.decisive_evidence_ids == ("status:16", "status:20", "status:25")
    assert "event-1" not in item.decisive_evidence_ids
    # The time basis still quotes the latest observation only.
    (basis,) = item.time_basis
    assert basis.evidence_ids == ("status:25",)


def test_terminated_records_the_instance_tombstone_only() -> None:
    body: dict[str, Any] = {"kind": "Pod", "metadata": {"name": POD.name, "uid": "u1"}}
    history = {
        POD: [
            ObjectVersion(
                entity=POD,
                observed_at=_minutes(-30),
                body=body,
                evidence_id="journal:1",
                lifecycle=Lifecycle.CREATED,
            ),
            ObjectVersion(
                entity=POD,
                observed_at=_minutes(-2),
                body=body,
                evidence_id="journal:9",
                lifecycle=Lifecycle.DELETED,
            ),
        ]
    }
    item = _ended_episode_elimination(_assess(history=history))
    assert item.decisive_evidence_ids == ("journal:9",)


def test_resource_mechanism_records_all_coverage_but_not_the_change() -> None:
    pods = tuple(EntityRef(namespace="shop", kind="Pod", name=f"api-{n}") for n in (1, 2))
    coverage = tuple(
        PodCoverage(
            pod=pod,
            container="api",
            resource="memory",
            window_start=_minutes(-3),
            window_end=_minutes(16),
            peak=0.4,
            evidence_id=f"read:{pod.name}:memory",
        )
        for pod in pods
    )
    mismatch = MechanismMismatch(
        hypothesis_id="hypothesis:api",
        actor=EntityRef(namespace="shop", kind="Deployment", name="api"),
        lowered=(("api", "memory"),),
        pods=pods,
        onset=ONSET,
        boundary=ONSET + GRACE,
        window_start=_minutes(-3),
        window_end=_minutes(16),
        coverage=coverage,
        change_evidence_ids=("journal:old", "journal:new"),
        preconditions=(EliminationPrecondition(name="lowered_only", passed=True),),
    )
    item = _mechanism_elimination(mismatch)

    assert item.decisive_evidence_ids == ("read:api-1:memory", "read:api-2:memory")
    assert not set(item.decisive_evidence_ids) & set(mismatch.change_evidence_ids)


def test_records_without_decisive_evidence_still_load() -> None:
    legacy = ResolutionElimination.model_validate(
        {"hypothesis_id": "hypothesis:old", "code": "EXPLICIT_TEMPORAL_CONTRADICTION"}
    )
    assert legacy.decisive_evidence_ids == ()
