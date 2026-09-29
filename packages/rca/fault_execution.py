"""Canonical chaos-execution records parsed from controller events.

One record per experiment or schedule *instance*: the event's involved UID is part of the
identity, so two incarnations that share a name are never merged. A record states what the
controller reported and nothing more: an ``Applied`` event names a target, a ``Recovered``
event closes that target's interval, and a missing ``Recovered`` is *unobserved*, not active.
Selectors, actions and target UIDs are not read here; they are not in the events.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from packages.rca.model import ClusterEvent, EntityRef
from packages.rca.topology import is_chaos_kind

_TARGET = r"(?P<ns>[a-z0-9.-]+)/(?P<pod>[a-z0-9.-]+)(?:/(?P<container>[a-z0-9._-]+))?"
_APPLIED = re.compile(r"Successfully apply chaos for " + _TARGET, re.IGNORECASE)
_RECOVERED = re.compile(r"Successfully recover chaos for " + _TARGET, re.IGNORECASE)
_SPAWNED = re.compile(r"Create new object: (?P<name>[a-z0-9.-]+)", re.IGNORECASE)
_FAILED_RECOVERY = re.compile(r"failed to recover", re.IGNORECASE)

FaultKey = tuple[EntityRef, str | None]


class FaultRef(BaseModel):
    """One chaos object instance: its name and the UID its events reported."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    uid: str | None = None


class FaultTarget(BaseModel):
    """One target the controller reported applying a fault to, with its interval."""

    model_config = ConfigDict(frozen=True)

    pod: EntityRef
    container: str | None = None
    applied_at: datetime | None = None
    recovered_at: datetime | None = None  # None: no recovery observed, not "still active"
    applications: int = 0
    applied_evidence: tuple[str, ...] = ()
    recovered_evidence: tuple[str, ...] = ()


class FaultExecution(BaseModel):
    """What the events report about one experiment or schedule instance."""

    model_config = ConfigDict(frozen=True)

    ref: FaultRef
    schedule: FaultRef | None = None  # the schedule instance that spawned this experiment
    children: tuple[FaultRef, ...] = ()  # experiment instances a schedule spawned
    targets: tuple[FaultTarget, ...] = ()
    reasons: dict[str, int]
    spawned: int = 0
    failed_apply: int = 0
    failed_recover: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    evidence_ids: tuple[str, ...] = ()


def _span(items: Sequence[ClusterEvent]) -> tuple[datetime | None, datetime | None]:
    times = [t for item in items for t in (item.first_at, item.last_at) if t is not None]
    return (min(times), max(times)) if times else (None, None)


def _targets(items: Sequence[ClusterEvent]) -> tuple[FaultTarget, ...]:
    applied: dict[tuple[EntityRef, str | None], list[ClusterEvent]] = defaultdict(list)
    recovered: dict[tuple[EntityRef, str | None], list[ClusterEvent]] = defaultdict(list)
    for event in items:
        for pattern, bucket, reason in (
            (_APPLIED, applied, "Applied"),
            (_RECOVERED, recovered, "Recovered"),
        ):
            match = pattern.search(event.message)
            if event.reason == reason and match is not None:
                pod = EntityRef(kind="Pod", name=match["pod"], namespace=match["ns"])
                bucket[(pod, match["container"])].append(event)
    result = []
    for key in sorted(applied, key=lambda k: (k[0].canonical, k[1] or "")):
        opened, closed = applied[key], recovered.get(key, [])
        opens = [t for e in opened if (t := e.first_at or e.last_at) is not None]
        closes = [t for e in closed if (t := e.last_at or e.first_at) is not None]
        result.append(
            FaultTarget(
                pod=key[0],
                container=key[1],
                applied_at=min(opens) if opens else None,
                recovered_at=max(closes) if closes else None,
                applications=len(opened),
                applied_evidence=tuple(e.evidence_id for e in opened),
                recovered_evidence=tuple(e.evidence_id for e in closed),
            )
        )
    return tuple(result)


def fault_executions(
    events: Sequence[ClusterEvent],
    parents: Mapping[EntityRef, EntityRef] | None = None,
) -> tuple[FaultExecution, ...]:
    """Canonical executions, one per (chaos object, involved UID).

    A schedule instance owns the experiments it named in ``Spawned`` events. Only when
    no such event names an experiment does ``parents`` (name-derived structure) supply
    the owner, and then without a schedule UID.
    """
    groups: dict[FaultKey, list[ClusterEvent]] = defaultdict(list)
    for event in events:
        if is_chaos_kind(event.entity.kind):
            groups[(event.entity, event.involved_uid)].append(event)
    by_name: dict[tuple[str, str], list[FaultKey]] = defaultdict(list)
    for key in groups:
        if key[0].kind != "Schedule":
            by_name[(key[0].namespace, key[0].name)].append(key)
    owner_of: dict[FaultKey, FaultRef] = {}
    children: dict[FaultKey, list[FaultRef]] = defaultdict(list)
    for key, items in groups.items():
        if key[0].kind != "Schedule":
            continue
        for event in items:
            match = _SPAWNED.search(event.message) if event.reason == "Spawned" else None
            if match is None:
                continue
            for child in by_name.get((key[0].namespace, match["name"]), ()):
                if child not in owner_of:
                    owner_of[child] = FaultRef(entity=key[0], uid=key[1])
                    children[key].append(FaultRef(entity=child[0], uid=child[1]))
    if parents:
        for key in groups:
            parent = parents.get(key[0])
            if key[0].kind != "Schedule" and key not in owner_of and parent is not None:
                owner_of[key] = FaultRef(entity=parent, uid=None)
    result = []
    for key, items in groups.items():
        first, last = _span(items)
        reasons: dict[str, int] = defaultdict(int)
        for item in items:
            reasons[item.reason] += 1
        failed = [e for e in items if e.reason == "Failed"]
        recover_failures = sum(1 for e in failed if _FAILED_RECOVERY.search(e.message))
        result.append(
            FaultExecution(
                ref=FaultRef(entity=key[0], uid=key[1]),
                schedule=owner_of.get(key),
                children=tuple(children.get(key, ())),
                targets=_targets(items) if key[0].kind != "Schedule" else (),
                reasons=dict(reasons),
                spawned=reasons.get("Spawned", 0),
                failed_apply=len(failed) - recover_failures,
                failed_recover=recover_failures,
                first_at=first,
                last_at=last,
                evidence_ids=tuple(e.evidence_id for e in items),
            )
        )
    return tuple(result)


__all__ = [
    "FaultExecution",
    "FaultRef",
    "FaultTarget",
    "fault_executions",
]
