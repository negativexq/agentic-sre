"""M19-2.3: Pod lifecycle transitions from two consecutive bodies."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from packages.rca.lifecycle import LifecycleObservationDraft, classify
from packages.storage.models import LIFECYCLE_OBSERVATION_TYPES

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pod(
    *,
    uid: str | None = "uid-a",
    ready: str | None = "True",
    since: str = "2026-09-25T11:50:00Z",
    containers: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    deletion: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": "web-0",
        "namespace": "shop",
        "creationTimestamp": "2026-09-25T11:40:00Z",
    }
    if uid is not None:
        metadata["uid"] = uid
    if deletion is not None:
        metadata["deletionTimestamp"] = deletion
    status: dict[str, Any] = {
        "conditions": (
            [{"type": "Ready", "status": ready, "lastTransitionTime": since}]
            if ready is not None
            else []
        ),
        "containerStatuses": containers
        if containers is not None
        else [_running("web", "2026-09-25T11:41:00Z")],
    }
    if reason is not None:
        status["reason"] = reason
    return {
        "kind": "Pod",
        "metadata": metadata,
        "spec": {"containers": [{"name": "web", "env": [{"name": "SECRET", "value": "x"}]}]},
        "status": status,
    }


def _running(name: str, started: str, restarts: int = 0, **last: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "restartCount": restarts,
        "state": {"running": {"startedAt": started}},
        "lastState": {"terminated": last} if last else {},
    }
    return item


def _types(drafts: list[LifecycleObservationDraft]) -> list[str]:
    return [item.type for item in drafts]


def test_first_sighting_is_observed_only_with_creation_time() -> None:
    (draft,) = classify(None, _pod(), NOW)

    assert draft.type == "OBSERVED"
    assert draft.source_at == _time("2026-09-25T11:40:00Z")
    assert (draft.namespace, draft.kind, draft.name, draft.instance_uid) == (
        "shop",
        "Pod",
        "web-0",
        "uid-a",
    )
    assert draft.observed_at == NOW


def test_same_body_twice_is_no_transition() -> None:
    body = _pod(containers=[_running("web", "2026-09-25T11:45:00Z", 1, reason="OOMKilled")])
    assert classify(body, copy.deepcopy(body), NOW) == []


def test_ready_transitions_use_last_transition_time() -> None:
    down = _pod(ready="False", since="2026-09-25T11:55:00Z")
    (draft,) = classify(_pod(), down, NOW)
    assert (draft.type, draft.source_at) == ("READY_FALSE", _time("2026-09-25T11:55:00Z"))

    up = _pod(ready="True", since="2026-09-25T11:56:00Z")
    (draft,) = classify(down, up, NOW)
    assert (draft.type, draft.source_at) == ("READY_TRUE", _time("2026-09-25T11:56:00Z"))


def test_ready_flip_between_observations_is_not_lost() -> None:
    # Still True, but the transition time moved: it went down and came back.
    later = _pod(since="2026-09-25T11:58:00Z")
    assert _types(classify(_pod(), later, NOW)) == ["READY_TRUE"]


def test_unknown_readiness_is_not_a_transition() -> None:
    assert classify(_pod(), _pod(ready="Unknown"), NOW) == []


def test_container_start_and_termination() -> None:
    crashed = _pod(
        containers=[
            {
                "name": "web",
                "restartCount": 0,
                "state": {
                    "terminated": {
                        "reason": "Error",
                        "exitCode": 1,
                        "finishedAt": "2026-09-25T11:52:00Z",
                        "containerID": "c1",
                    }
                },
                "lastState": {},
            }
        ]
    )
    (draft,) = classify(_pod(), crashed, NOW)
    assert (draft.type, draft.source_at) == ("CONTAINER_TERMINATED", _time("2026-09-25T11:52:00Z"))
    assert draft.payload["containers"] == ["web"]

    # The same termination moves to lastState on restart: only the start is new.
    restarted = _pod(
        containers=[
            _running(
                "web",
                "2026-09-25T11:53:00Z",
                1,
                reason="Error",
                exitCode=1,
                finishedAt="2026-09-25T11:52:00Z",
                containerID="c1",
            )
        ]
    )
    (draft,) = classify(crashed, restarted, NOW)
    assert (draft.type, draft.source_at) == ("CONTAINER_STARTED", _time("2026-09-25T11:53:00Z"))


def test_oom_kill_in_last_state() -> None:
    oom = _pod(
        containers=[
            _running(
                "web",
                "2026-09-25T11:54:00Z",
                1,
                reason="OOMKilled",
                exitCode=137,
                finishedAt="2026-09-25T11:53:30Z",
                containerID="c1",
            )
        ]
    )
    assert _types(classify(_pod(), oom, NOW)) == ["OOM_KILLED", "CONTAINER_STARTED"]
    oom_draft = classify(_pod(), oom, NOW)[0]
    assert oom_draft.source_at == _time("2026-09-25T11:53:30Z")


def test_eviction_and_deletion_request() -> None:
    evicted = _pod(reason="Evicted")
    (draft,) = classify(_pod(), evicted, NOW)
    assert (draft.type, draft.source_at) == ("EVICTED", None)

    deleting = _pod(deletion="2026-09-25T11:59:00Z")
    (draft,) = classify(_pod(), deleting, NOW)
    assert (draft.type, draft.source_at) == ("DELETION_REQUESTED", _time("2026-09-25T11:59:00Z"))


def test_body_without_uid_yields_nothing() -> None:
    assert classify(None, _pod(uid=None), NOW) == []
    assert classify(_pod(), _pod(uid=None, ready="False"), NOW) == []


def test_previous_body_of_another_uid_is_not_this_instance() -> None:
    reincarnated = _pod(uid="uid-b", ready="False")
    assert _types(classify(_pod(uid="uid-a"), reincarnated, NOW)) == ["OBSERVED"]


def test_payload_keeps_only_status_facts() -> None:
    down = _pod(ready="False", since="2026-09-25T11:55:00Z", deletion="2026-09-25T11:59:00Z")
    for draft in classify(_pod(), down, NOW):
        assert set(draft.payload) == {"ready", "containerStatuses", "deletionTimestamp"}
        assert "SECRET" not in repr(draft.payload)
        assert set(draft.payload["containerStatuses"][0]) == {
            "name",
            "restartCount",
            "state",
            "lastState",
        }


def test_only_ledger_types_are_produced_and_never_created() -> None:
    bodies = [
        _pod(),
        _pod(ready="False", since="2026-09-25T11:55:00Z"),
        _pod(reason="Evicted", deletion="2026-09-25T11:59:00Z"),
    ]
    produced = {item.type for item in classify(None, bodies[0], NOW)}
    for previous, current in zip(bodies, bodies[1:], strict=False):
        produced |= {item.type for item in classify(previous, current, NOW)}
    assert produced <= set(LIFECYCLE_OBSERVATION_TYPES)
    assert "CREATED" not in produced
