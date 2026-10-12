"""Service-level effect relation (m21 causal semantics contract §12, approved for shadow measurement).

Does an execution at an exact target pod show as an effect at a declared symptom service? Read from paired calls of
the captured traces: the caller's client span and the target's server span (its parent link). Two forms: non-success
outcomes that appear in the execution interval and not before it, and latency far above the caller's own baseline.
Too few calls on a side leaves the relation unknown (``None``), never false. Nothing in the engine calls it yet.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.rca.model import (
    EntityRef,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
    TraceSpanObservation,
)
from packages.rca.signals import _creation_time


@dataclass(frozen=True)
class EffectParameters:
    calls: int  # N: paired calls needed on each side
    factor: float  # F: the fault calls' median above F x the baseline's median
    floor: timedelta  # D: and above this in absolute terms


@dataclass(frozen=True)
class CallPair:
    started: datetime
    duration: timedelta
    target_pod: str
    non_success: bool
    evidence_ids: tuple[str, ...] = ()


# §12.6: the most permissive grid point with no hard violation on DEV, to be confirmed on a new HOLDOUT
SERVICE_EFFECT = EffectParameters(calls=3, factor=3.0, floor=timedelta(seconds=0.2))
# the trace read's baseline window, relative to the incident's onset (live-trace-design.md §9)
BASELINE_FROM, BASELINE_TO = timedelta(minutes=10), timedelta(minutes=5)
# m21 §16: the relation's baseline is the calls that began this long before the fault's first execution
BASELINE_BEFORE_EXECUTION = timedelta(minutes=5)


def baseline_before(first_execution: datetime) -> tuple[datetime, datetime]:
    """The baseline window ending just before ``first_execution`` (calls that began at it are not baseline)."""
    return (
        first_execution - BASELINE_BEFORE_EXECUTION,
        first_execution - timedelta(microseconds=1),
    )


def _duration(span: TraceSpanObservation) -> timedelta | None:
    return span.end_at - span.start_at if span.end_at is not None else None


def _non_success(span: TraceSpanObservation) -> bool:
    code = span.semantic_attributes.get("http.response.status_code", "")
    return str(span.status).endswith("ERROR") or (code.isdigit() and int(code) >= 500)


def call_pairs(
    spans: Sequence[TraceSpanObservation], caller_service: str, target_service: str
) -> list[CallPair]:
    """Client spans of ``caller_service`` whose child is a server span of ``target_service``."""
    servers = {
        (s.trace_id, s.parent_span_id): s
        for s in spans
        if s.service == target_service and s.span_kind == "SERVER" and s.parent_span_id
    }
    pairs = []
    for client in spans:
        if client.service != caller_service or client.span_kind != "CLIENT":
            continue
        server = servers.get((client.trace_id, client.span_id))
        duration = _duration(client)
        if server is None or duration is None:
            continue
        pairs.append(
            CallPair(
                client.start_at,
                duration,
                server.semantic_attributes.get("k8s.pod.name", ""),
                _non_success(client) or _non_success(server),
                (client.evidence_id, server.evidence_id),
            )
        )
    return pairs


def fault_calls(
    pairs: Sequence[CallPair], *, target_pod: str, start: datetime, end: datetime
) -> list[CallPair]:
    return [p for p in pairs if p.target_pod == target_pod and start <= p.started <= end]


def service_effect(
    pairs: Sequence[CallPair],
    *,
    target_pod: str,
    start: datetime,
    end: datetime,
    baseline: tuple[datetime, datetime],
    parameters: EffectParameters,
) -> bool | None:
    """§12.2 for one witness target and one symptom service, from that service's calls to the target's service.

    Fault calls go to the exact ``target_pod`` and begin inside ``[start, end]``; baseline calls begin inside the
    run's ``baseline`` window, which lies before every execution of the incident (an execution acts seconds before
    its ``Applied`` is recorded, and a recurring fault slows the calls between its executions too). The baseline is
    read by its median, which an earlier, unrelated slowdown in the window cannot move unless it fills half of it
    (§12.5). ``None`` when either side has fewer than ``parameters.calls`` calls, or when the baseline itself is
    already slow (its median above the floor ``D``): such a window cannot show a rise.
    """
    fault = [p for p in pairs if p.target_pod == target_pod and start <= p.started <= end]
    calm = [p for p in pairs if baseline[0] <= p.started <= baseline[1]]
    if len(fault) < parameters.calls or len(calm) < parameters.calls:
        return None
    if any(p.non_success for p in fault) and not any(p.non_success for p in calm):
        return True
    fault_median = statistics.median(p.duration.total_seconds() for p in fault)
    baseline_median = statistics.median(p.duration.total_seconds() for p in calm)
    if baseline_median > parameters.floor.total_seconds():
        return None
    return (
        fault_median > parameters.factor * baseline_median
        and fault_median > parameters.floor.total_seconds()
    )


# ---- m21 §23: calls that never reached the target ------------------------------------------------


@dataclass(frozen=True)
class UnansweredCall:
    started: datetime
    evidence_id: str


def unanswered_calls(
    spans: Sequence[TraceSpanObservation], caller_service: str, target_service: str
) -> list[UnansweredCall]:
    """Client spans of ``caller_service`` addressed to ``target_service`` that ended in a non-success outcome with
    no server span under them in the same fetched trace. A span still open has no outcome and is not one (§23.5)."""
    answered = {(s.trace_id, s.parent_span_id) for s in spans if s.span_kind == "SERVER"}
    return [
        UnansweredCall(span.start_at, span.evidence_id)
        for span in spans
        if span.service == caller_service
        and span.span_kind == "CLIENT"
        and span.semantic_attributes.get("server.address") == target_service
        and span.end_at is not None
        and _non_success(span)
        and (span.trace_id, span.span_id) not in answered
    ]


def _owner_uid(version: ObjectVersion) -> str | None:
    metadata = version.body.get("metadata")
    owners = metadata.get("ownerReferences") if isinstance(metadata, dict) else None
    for owner in owners if isinstance(owners, list) else ():
        if isinstance(owner, dict) and owner.get("controller") is not False and owner.get("uid"):
            return str(owner["uid"])
    return None


def _presence(versions: Sequence[ObjectVersion]) -> tuple[datetime, datetime | None]:
    created = min(_creation_time(v.body) or v.observed_at for v in versions)
    deleted = next((v.observed_at for v in versions if v.lifecycle is Lifecycle.DELETED), None)
    return created, deleted


def _name_holder(versions: Sequence[ObjectVersion], start: datetime, end: datetime) -> str | None:
    """The one UID the journal shows holding a pod name at some time in ``[start, end]`` (§21 Rule B's identity,
    over an interval the pod may have joined or left); ``None`` for no holder, several, or a version without a UID."""
    if any(v.instance_uid is None for v in versions):
        return None
    holders = {
        uid
        for uid in {v.instance_uid for v in versions}
        for created, deleted in (_presence([v for v in versions if v.instance_uid == uid]),)
        if created <= end and (deleted is None or deleted >= start)
    }
    return holders.pop() if len(holders) == 1 else None


def _replicas(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    target: EntityRef,
    start: datetime,
    end: datetime,
) -> tuple[str, dict[tuple[EntityRef, str], list[ObjectVersion]]] | None:
    """m21 §34: the target's UID and every other pod of its workload the journal places in ``[start, end]``.

    The workload is the target's owning ReplicaSet's own owner (else that ReplicaSet), by UID. ``None`` when the
    journal cannot place the target, its owner, or the owner's record.
    """
    versions = history.get(target)
    uid = _name_holder(versions or (), start, end)
    mine = next((v for v in versions or () if v.instance_uid == uid and _owner_uid(v)), None)
    owner = _owner_uid(mine) if mine is not None else None
    if uid is None or owner is None:
        return None
    records = [
        v for ref, vs in history.items() if ref.kind == "ReplicaSet" for v in vs if v.instance_uid
    ]
    if not any(v.instance_uid == owner for v in records):
        return None
    workload = next(
        (_owner_uid(v) for v in records if v.instance_uid == owner and _owner_uid(v)), owner
    )
    owners = {owner} | {
        v.instance_uid
        for v in records
        if v.entity.namespace == target.namespace and _owner_uid(v) == workload and v.instance_uid
    }
    replicas: dict[tuple[EntityRef, str], list[ObjectVersion]] = {}
    for ref, vs in history.items():
        if ref.kind != "Pod" or ref.namespace != target.namespace:
            continue
        for version in vs:
            other = version.instance_uid
            if other and other != uid and _owner_uid(version) in owners:
                replicas.setdefault((ref, other), []).append(version)
    present = {}
    for key, vs in replicas.items():
        created, deleted = _presence(vs)
        if created <= end and (deleted is None or deleted >= start):
            present[key] = vs
    return uid, present


def other_server_ready(
    statuses: Sequence[PodStatusObservation],
    target_pod: str,
    namespace: str,
    start: datetime,
    end: datetime,
    history: Mapping[EntityRef, Sequence[ObjectVersion]] | None = None,
) -> bool | None:
    """Whether a pod of ``target_pod``'s workload other than it may have been ``Ready`` in ``[start, end]``.

    m21 §34: the workload's pods are those the journal places by UID ownership, not by name. ``True`` when the
    ledger shows one of them ``Ready`` inside the interval, or its last observation before the interval ``Ready``.
    ``False`` only when the ledger shows the target, and each other placed pod has a known not-``Ready`` state at or
    before ``start`` and no unknown state during the interval. Anything else, a target or workload the journal
    cannot place among them, is unknown (``None``): a missing record is not a pod that was not ``Ready`` (§3 I1).
    """
    target = EntityRef(kind="Pod", name=target_pod, namespace=namespace)
    if not any(s.pod == target for s in statuses):
        return None
    placed = _replicas(history or {}, target, start, end)
    if placed is None:
        return None
    unknown = False
    for (pod, uid), _ in placed[1].items():
        observations = [s for s in statuses if s.pod == pod and s.uid in (None, uid)]
        if any(
            s.ready is True and s.observed_at >= start and (s.ready_since or s.observed_at) <= end
            for s in observations
        ):
            return True
        before = [s for s in observations if s.observed_at < start]
        if before and max(before, key=lambda s: s.observed_at).ready is True:
            return True
        at_start = [s for s in observations if s.observed_at <= start]
        latest_at = max((s.observed_at for s in at_start), default=None)
        latest = [s for s in at_start if s.observed_at == latest_at]
        if not latest or any(s.ready is not False for s in latest):
            unknown = True
        elif any(
            s.ready is None and s.observed_at >= start and (s.ready_since or s.observed_at) <= end
            for s in observations
        ):
            unknown = True
    return None if unknown else False


def unanswered_effect(
    pairs: Sequence[CallPair],
    unanswered: Sequence[UnansweredCall],
    *,
    target_pod: str,
    start: datetime,
    end: datetime,
    baseline: tuple[datetime, datetime],
    other_ready: bool | None,
    parameters: EffectParameters,
) -> bool | None:
    """§23.2, the third form, for one witness target and one symptom service.

    At least ``parameters.calls`` unanswered calls inside ``[start, end]``; at least as many paired calls in the
    ``baseline`` and no unanswered one there; and the target the only server those calls could have reached (every
    paired call from the baseline's start to ``end`` answered by it, and no other pod of its workload ``Ready``).
    ``None`` whenever one of these cannot be shown: an unanswered call has no pod of its own.
    """
    fault = [u for u in unanswered if start <= u.started <= end]
    calm = [p for p in pairs if baseline[0] <= p.started <= baseline[1]]
    if len(fault) < parameters.calls or len(calm) < parameters.calls:
        return None
    if any(baseline[0] <= u.started <= baseline[1] for u in unanswered):
        return None
    if any(p.target_pod != target_pod for p in pairs if baseline[0] <= p.started <= end):
        return None
    if other_ready is not False:
        return None
    return True
