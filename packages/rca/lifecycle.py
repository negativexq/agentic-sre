"""Pod lifecycle transitions read from two consecutive observed bodies.

``classify`` is pure: it compares what one exact Pod instance showed at the
previous observation with what it shows now and returns only the transitions
the bodies themselves prove. It never looks anything up. A body without a UID
yields nothing, and a previous body of another UID under the same name is not
this instance's history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from packages.rca.json_access import child, mapping


@dataclass(frozen=True)
class LifecycleObservationDraft:
    """A lifecycle fact ready to be appended to the ledger."""

    namespace: str
    kind: str
    name: str
    instance_uid: str
    type: str
    source_at: datetime | None
    observed_at: datetime
    payload: dict[str, Any]


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _uid(body: Mapping[str, Any] | None) -> str | None:
    uid = child(body or {}, "metadata").get("uid")
    return uid if isinstance(uid, str) and uid else None


def _ready(body: Mapping[str, Any] | None) -> dict[str, Any] | None:
    for item in child(body or {}, "status").get("conditions") or []:
        condition = mapping(item)
        if condition.get("type") == "Ready":
            return condition
    return None


def _containers(body: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("name")): item
        for item in map(mapping, child(body or {}, "status").get("containerStatuses") or [])
        if item.get("name")
    }


def _payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """Only the Ready condition, container restart/state facts and deletionTimestamp."""
    ready = _ready(body)
    return {
        "ready": dict(ready) if ready is not None else None,
        "containerStatuses": [
            {
                "name": name,
                "restartCount": item.get("restartCount"),
                "state": item.get("state"),
                "lastState": item.get("lastState"),
            }
            for name, item in sorted(_containers(body).items())
        ],
        "deletionTimestamp": child(body, "metadata").get("deletionTimestamp"),
    }


def _terminations(item: Mapping[str, Any] | None) -> dict[tuple[Any, Any], dict[str, Any]]:
    """Every termination a container status shows, keyed by its own identity."""
    found: dict[tuple[Any, Any], dict[str, Any]] = {}
    for holder in ("state", "lastState"):
        terminated = mapping(child(item or {}, holder).get("terminated"))
        if terminated:
            found[(terminated.get("finishedAt"), terminated.get("containerID"))] = terminated
    return found


def classify(
    previous_body: Mapping[str, Any] | None,
    current_body: Mapping[str, Any],
    observed_at: datetime,
) -> list[LifecycleObservationDraft]:
    """Transitions of one Pod instance between two observations, in a fixed order."""
    uid = _uid(current_body)
    metadata = child(current_body, "metadata")
    if current_body.get("kind") != "Pod" or uid is None:
        return []
    if _uid(previous_body) != uid:
        previous_body = None
    payload = _payload(current_body)

    def draft(
        type_: str, source_at: datetime | None, extra: dict[str, Any] | None = None
    ) -> LifecycleObservationDraft:
        return LifecycleObservationDraft(
            namespace=str(metadata.get("namespace") or ""),
            kind="Pod",
            name=str(metadata.get("name") or ""),
            instance_uid=uid,
            type=type_,
            source_at=source_at,
            observed_at=observed_at,
            payload=payload | (extra or {}),
        )

    if previous_body is None:
        return [draft("OBSERVED", _time(metadata.get("creationTimestamp")))]

    drafts: list[LifecycleObservationDraft] = []
    deletion = metadata.get("deletionTimestamp")
    if deletion and not child(previous_body, "metadata").get("deletionTimestamp"):
        drafts.append(draft("DELETION_REQUESTED", _time(deletion)))

    evicted = child(current_body, "status").get("reason") == "Evicted"
    if evicted and child(previous_body, "status").get("reason") != "Evicted":
        drafts.append(draft("EVICTED", None))

    before, now = _ready(previous_body) or {}, _ready(current_body) or {}
    status = now.get("status")
    if status in ("True", "False") and (
        status != before.get("status")
        or now.get("lastTransitionTime") != before.get("lastTransitionTime")
    ):
        drafts.append(
            draft(
                "READY_TRUE" if status == "True" else "READY_FALSE",
                _time(now.get("lastTransitionTime")),
            )
        )

    events: dict[str, list[tuple[str, datetime | None]]] = {}
    previous_containers = _containers(previous_body)
    for name, item in sorted(_containers(current_body).items()):
        old = previous_containers.get(name)
        seen = _terminations(old)
        for key, terminated in _terminations(item).items():
            if key in seen:
                continue
            type_ = (
                "OOM_KILLED" if terminated.get("reason") == "OOMKilled" else "CONTAINER_TERMINATED"
            )
            events.setdefault(type_, []).append((name, _time(terminated.get("finishedAt"))))
        started = child(child(item, "state"), "running").get("startedAt")
        previous_start = child(child(old or {}, "state"), "running").get("startedAt")
        if started and started != previous_start:
            events.setdefault("CONTAINER_STARTED", []).append((name, _time(started)))
    # One fact per type per observation, naming every container it covers.
    for type_ in ("CONTAINER_TERMINATED", "OOM_KILLED", "CONTAINER_STARTED"):
        items = events.get(type_)
        if not items:
            continue
        times = [at for _, at in items if at is not None]
        drafts.append(
            draft(
                type_,
                min(times) if times else None,
                {"containers": sorted({name for name, _ in items})},
            )
        )
    return drafts


__all__ = ["LifecycleObservationDraft", "classify"]
