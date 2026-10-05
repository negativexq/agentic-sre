"""Service-level effect relation (m21 causal semantics contract §12, approved for shadow measurement).

Does an execution at an exact target pod show as an effect at a declared symptom service? Read from paired calls of
the captured traces: the caller's client span and the target's server span (its parent link). Two forms: non-success
outcomes that appear in the execution interval and not before it, and latency far above the caller's own baseline.
Too few calls on a side leaves the relation unknown (``None``), never false. Nothing in the engine calls it yet.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.rca.model import TraceSpanObservation


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
