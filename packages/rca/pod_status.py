"""Typed Pod status observations read from Kubernetes object bodies.

Status is not part of the object journal's desired-state versions, so every
body that was actually observed is also read here for what its status showed
at that moment. Nothing is inferred for moments without an observed body.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.rca.json_access import child, mapping
from packages.rca.model import EntityRef, ObjectVersion, PodStatusObservation


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pod_status_from_body(
    pod: EntityRef, body: Mapping[str, Any], observed_at: datetime, evidence_id: str
) -> PodStatusObservation:
    """Read the Ready condition exactly as the body reported it."""
    ready: bool | None = None
    ready_since: datetime | None = None
    for item in child(body, "status").get("conditions") or []:
        condition = mapping(item)
        if condition.get("type") != "Ready":
            continue
        status = condition.get("status")
        ready = True if status == "True" else False if status == "False" else None
        ready_since = _time(condition.get("lastTransitionTime"))
    uid = child(body, "metadata").get("uid")
    return PodStatusObservation(
        pod=pod,
        uid=uid if isinstance(uid, str) and uid else None,
        observed_at=observed_at,
        ready=ready,
        ready_since=ready_since,
        evidence_id=evidence_id,
    )


def pod_status_from_history(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> tuple[PodStatusObservation, ...]:
    """One status observation per observed Pod version, tombstones excluded."""
    return tuple(
        pod_status_from_body(entity, version.body, version.observed_at, version.evidence_id)
        for entity, versions in history.items()
        if entity.kind == "Pod"
        for version in versions
        if version.lifecycle.value != "DELETED"
    )


def ordered(observations: Iterable[PodStatusObservation]) -> tuple[PodStatusObservation, ...]:
    """Deduplicate and order by pod then observation time.

    Evidence ids alone are not unique (``cluster:current`` names a listing, not
    one object), so the pod and time are part of the key.
    """
    unique = {(item.pod, item.observed_at, item.evidence_id): item for item in observations}
    return tuple(sorted(unique.values(), key=lambda item: (item.pod.canonical, item.observed_at)))


__all__ = ["ordered", "pod_status_from_body", "pod_status_from_history"]
