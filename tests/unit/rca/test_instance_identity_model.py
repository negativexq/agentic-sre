from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.rca.model import (
    ClusterEvent,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    ObjectVersion,
)

ENTITY_A = EntityRef(namespace="sre-demo", kind="Pod", name="payment-abc")
ENTITY_B = EntityRef(namespace="sre-demo", kind="Pod", name="order-xyz")
OBSERVED_AT = datetime(2026, 9, 25, tzinfo=UTC)


def _finding(
    *, entity: EntityRef = ENTITY_A, entity_instance: EntityInstanceRef | None = None
) -> Finding:
    return Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=entity,
        entity_instance=entity_instance,
        at=None,
        summary="Pod failure event",
    )


def test_entity_instance_ref_accepts_non_empty_uid_and_is_frozen() -> None:
    instance = EntityInstanceRef(entity=ENTITY_A, uid="uid-123")

    assert instance.entity == ENTITY_A
    assert instance.uid == "uid-123"
    with pytest.raises(ValidationError):
        instance.uid = "uid-other"


def test_entity_instance_ref_rejects_empty_uid() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        EntityInstanceRef(entity=ENTITY_A, uid="")


def test_finding_accepts_matching_entity_instance() -> None:
    instance = EntityInstanceRef(entity=ENTITY_A, uid="uid-A")

    finding = _finding(entity_instance=instance)

    assert finding.entity == ENTITY_A
    assert finding.entity_instance == instance


def test_finding_rejects_mismatched_entity_instance() -> None:
    instance = EntityInstanceRef(entity=ENTITY_B, uid="uid-B")

    with pytest.raises(ValidationError, match="entity_instance.entity must match entity"):
        _finding(entity=ENTITY_A, entity_instance=instance)


def test_finding_without_instance_remains_backward_compatible() -> None:
    finding = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=ENTITY_A,
        at=None,
        summary="Pod failure event",
    )

    assert finding.entity_instance is None
    old_payload = finding.model_dump(mode="python")
    old_payload.pop("entity_instance")
    assert Finding.model_validate(old_payload).entity_instance is None


def test_cluster_event_uid_is_optional_and_preserved() -> None:
    event = ClusterEvent(entity=ENTITY_A, reason="Killing", evidence_id="event:1")
    event_with_uid = ClusterEvent(
        entity=ENTITY_A, involved_uid="uid-A", reason="Killing", evidence_id="event:2"
    )

    assert event.involved_uid is None
    assert event_with_uid.involved_uid == "uid-A"
    old_payload = event.model_dump(mode="python")
    old_payload.pop("involved_uid")
    assert ClusterEvent.model_validate(old_payload).involved_uid is None


def test_object_version_uid_is_optional_and_preserved() -> None:
    version = ObjectVersion(
        entity=ENTITY_A,
        observed_at=OBSERVED_AT,
        body={},
        evidence_id="journal:1",
    )
    version_with_uid = ObjectVersion(
        entity=ENTITY_A,
        uid="uid-A",
        observed_at=OBSERVED_AT,
        body={},
        evidence_id="journal:2",
    )

    assert version.uid is None
    assert version_with_uid.uid == "uid-A"
    old_payload = version.model_dump(mode="python")
    old_payload.pop("uid")
    assert ObjectVersion.model_validate(old_payload).uid is None
