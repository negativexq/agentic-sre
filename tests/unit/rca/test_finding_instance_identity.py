"""Findings carry the exact Pod instance only when their own evidence names it."""

from __future__ import annotations

from typing import Any

from rca_builders import at, event, ref, version

from packages.rca.model import (
    ClusterEvent,
    EntityInstanceRef,
    FindingKind,
    ObjectVersion,
    ResourcePressure,
)
from packages.rca.signals import container_findings, failure_findings, resource_findings

POD = "shop/Pod/web-0"


def _warning(minutes: float, uid: str | None, reason: str = "Unhealthy") -> ClusterEvent:
    base = event(POD, reason, minutes, type_="Warning")
    return base.model_copy(update={"involved_uid": uid, "evidence_id": f"{base.evidence_id}:{uid}"})


def _crashing_pod(minutes: float, uid: str | None, index: int = 0) -> ObjectVersion:
    body: dict[str, Any] = {
        "status": {
            "containerStatuses": [
                {
                    "name": "web",
                    "restartCount": 2,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                }
            ]
        }
    }
    if uid is not None:
        body["metadata"] = {"uid": uid}
    return version(POD, minutes, body, index)


def test_failure_event_carries_the_involved_uid() -> None:
    [finding] = failure_findings([_warning(1, "uid-a")])
    assert finding.entity_instance == EntityInstanceRef(entity=ref(POD), uid="uid-a")


def test_failure_event_without_uid_has_no_instance() -> None:
    [finding] = failure_findings([_warning(1, None)])
    assert finding.entity_instance is None


def test_same_name_events_of_two_uids_never_share_a_finding() -> None:
    first, second = _warning(1, "uid-a"), _warning(2, "uid-b")
    findings = failure_findings([first, second])

    assert len(findings) == 2
    by_uid = {f.entity_instance.uid: f for f in findings if f.entity_instance is not None}
    assert set(by_uid) == {"uid-a", "uid-b"}
    assert by_uid["uid-a"].evidence_ids == (first.evidence_id,)
    assert by_uid["uid-b"].evidence_ids == (second.evidence_id,)
    assert all(f.entity == ref(POD) for f in findings)


def test_uid_less_events_are_not_merged_into_a_uid_group() -> None:
    bound, unbound = _warning(1, "uid-a"), _warning(2, None)
    findings = failure_findings([bound, unbound])

    assert {
        (f.entity_instance.uid if f.entity_instance else None, f.evidence_ids) for f in findings
    } == {("uid-a", (bound.evidence_id,)), (None, (unbound.evidence_id,))}


def test_container_failure_carries_the_uid_of_its_own_body() -> None:
    [finding] = container_findings({ref(POD): [_crashing_pod(1, "uid-b")]})
    assert finding.kind is FindingKind.CONTAINER_FAILURE
    assert finding.entity_instance == EntityInstanceRef(entity=ref(POD), uid="uid-b")


def test_container_failure_does_not_borrow_a_uid_from_older_versions() -> None:
    history = {ref(POD): [_crashing_pod(1, "uid-a", 0), _crashing_pod(2, None, 1)]}
    [finding] = container_findings(history)
    assert finding.entity_instance is None


def test_resource_pressure_is_still_produced_without_an_instance() -> None:
    pressure = ResourcePressure(
        pod=ref(POD),
        container="web",
        resource="memory",
        baseline=0.4,
        peak=0.95,
        at=at(3),
        evidence_id="metrics:memory",
    )
    [finding] = resource_findings([pressure])
    assert finding.kind is FindingKind.RESOURCE_PRESSURE
    assert finding.entity_instance is None
