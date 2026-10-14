"""Bounded positive request-pair observations; no RCA or exclusion authority."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    TraceSpanObservation,
    TraceSpanStatus,
)


class RequestPairTraceMissing(RuntimeError):
    """An explicit provider lookup returned no trace, never negative evidence."""


class RequestPairAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_log_id: str
    state: Literal["CORROBORATED", "UNKNOWN"]
    deficits: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    retrieval: Literal["REUSED", "FETCHED", "NONE"] = "NONE"


class RequestPairBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    spans: tuple[TraceSpanObservation, ...] = ()
    assessments: tuple[RequestPairAssessment, ...] = ()
    attempted_trace_ids: tuple[str, ...] = ()
    returned_span_count: int = 0


def valid_identity(value: str | None, width: int) -> bool:
    return bool(value and re.fullmatch(rf"[0-9a-f]{{{width}}}", value) and int(value, 16))


def pair_deficits(
    log: LogRecord,
    spans: Sequence[TraceSpanObservation],
    target: EntityRef,
    query: InvestigationQuery,
) -> tuple[str, ...]:
    attrs = log.attributes
    tid = attrs.get("trace_id")
    cid = attrs.get("span_id")
    pid = attrs.get("parent_span_id")
    clients = [s for s in spans if s.trace_id == tid and s.span_id == cid]
    parents = [s for s in spans if s.trace_id == tid and s.span_id == pid]
    if len(clients) != 1 or len(parents) != 1:
        return ("EXACT_CLIENT_PARENT_PAIR_MISSING_OR_NON_UNIQUE",)
    client, parent = clients[0], parents[0]
    deficits = []
    if (
        client.span_kind not in {"CLIENT", "SPAN_KIND_CLIENT"}
        or parent.span_kind not in {"SERVER", "SPAN_KIND_SERVER"}
        or client.parent_span_id != parent.span_id
        or cid == pid
    ):
        deficits.append("DIRECT_CLIENT_SERVER_PARENT_MISMATCH")
    if len({log.evidence_id, client.evidence_id, parent.evidence_id}) != 3:
        deficits.append("EVIDENCE_IDENTITIES_NOT_DISTINCT")
    key = {
        "Pod": "k8s.pod.name",
        "Deployment": "k8s.deployment.name",
        "StatefulSet": "k8s.statefulset.name",
        "DaemonSet": "k8s.daemonset.name",
    }.get(target.kind)
    for span in (client, parent):
        if (
            key is None
            or span.semantic_attributes.get("k8s.namespace.name") != target.namespace
            or span.semantic_attributes.get(key) != target.name
        ):
            deficits.append("KUBERNETES_TARGET_MISMATCH")
        if span.service != log.service:
            deficits.append("CALLER_MISMATCH")
        if not attrs.get("request_id") or span.semantic_attributes.get(
            "http.request.header.x_request_id"
        ) != attrs.get("request_id"):
            deficits.append("REQUEST_ID_MISSING_OR_MISMATCHED")
    for name in ("server.address", "error.type", "http.request.method"):
        if not attrs.get(name) or client.semantic_attributes.get(name) != attrs[name]:
            deficits.append("CLIENT_PEER_FAILURE_OR_METHOD_MISMATCH")
    if client.status is not TraceSpanStatus.ERROR:
        deficits.append("CLIENT_FAILURE_NOT_RECORDED")
    if not re.fullmatch(
        r"5[0-9]{2}", parent.semantic_attributes.get("http.response.status_code", "")
    ):
        deficits.append("PARENT_5XX_NOT_RECORDED")
    times = (
        query.start,
        query.end,
        log.at,
        client.start_at,
        client.end_at,
        parent.start_at,
        parent.end_at,
    )
    if any(at is None or at.utcoffset() is None for at in times):
        deficits.append("OPERATION_TIME_MISSING_OR_UNZONED")
    else:
        ordered = [
            t
            for t in (
                query.start,
                parent.start_at,
                client.start_at,
                log.at,
                client.end_at,
                parent.end_at,
                query.end,
            )
            if t is not None
        ]
        if any(left > right for left, right in zip(ordered, ordered[1:], strict=False)):
            deficits.append("OPERATION_OUTSIDE_NESTED_QUERY_WINDOW")
    return tuple(sorted(set(deficits)))


def read_request_pairs(
    target: EntityRef,
    query: InvestigationQuery,
    logs: Sequence[LogRecord],
    existing: Sequence[TraceSpanObservation],
    fetch: Callable[[str], tuple[TraceSpanObservation, ...]],
) -> RequestPairBatch:
    if (
        query.start is None
        or query.end is None
        or query.start.utcoffset() is None
        or query.end.utcoffset() is None
        or not 0 <= (query.end - query.start).total_seconds() <= 3600
    ):
        raise ValueError("Request pairs require a zoned query window <=3600 seconds")
    if (
        not query.source_log_ids
        or len(query.source_log_ids) > 8
        or not 2 <= query.limit <= 32
        or query.contains
        or query.reasons
    ):
        raise ValueError(
            "Request pairs require 1..8 explicit source log IDs and a 2..32 span limit"
        )
    if tuple(r.evidence_id for r in logs) != query.source_log_ids or len(
        set(query.source_log_ids)
    ) != len(logs):
        raise ValueError("Exact ordered source log membership required")
    selected: dict[tuple[str, str], TraceSpanObservation] = {}
    assessments: list[RequestPairAssessment] = []
    fetched: dict[str, tuple[TraceSpanObservation, ...] | None] = {}
    fetch_errors: dict[str, str] = {}
    raw_count = 0
    for log in logs:
        attrs = log.attributes
        tid = attrs.get("trace_id")
        cid = attrs.get("span_id")
        pid = attrs.get("parent_span_id")
        if not (valid_identity(tid, 32) and valid_identity(cid, 16) and valid_identity(pid, 16)):
            assessments.append(
                RequestPairAssessment(
                    source_log_id=log.evidence_id,
                    state="UNKNOWN",
                    deficits=("LOG_IDENTITIES_MISSING_OR_INVALID",),
                )
            )
            continue
        assert tid is not None
        if (
            log.at is None
            or log.at.utcoffset() is None
            or not query.start <= log.at <= query.end
            or not all(
                attrs.get(k)
                for k in ("request_id", "server.address", "error.type", "http.request.method")
            )
        ):
            assessments.append(
                RequestPairAssessment(
                    source_log_id=log.evidence_id,
                    state="UNKNOWN",
                    deficits=("SOURCE_LOG_SCOPE_OR_ATTRIBUTES_INCOMPLETE",),
                )
            )
            continue
        if len(selected) + 2 > query.limit and not all(
            (tid, sid) in selected for sid in (cid, pid)
        ):
            assessments.append(
                RequestPairAssessment(
                    source_log_id=log.evidence_id,
                    state="UNKNOWN",
                    deficits=("PAIR_SPAN_BUDGET_EXHAUSTED",),
                )
            )
            continue
        available = tuple(s for s in existing if s.trace_id == tid)
        retrieval: Literal["REUSED", "FETCHED", "NONE"] = "REUSED"
        deficits = pair_deficits(log, available, target, query)
        if deficits:
            if tid not in fetched:
                try:
                    fetched[tid] = fetch(tid)
                    raw_count += len(fetched[tid] or ())
                except Exception as error:
                    fetched[tid] = None
                    fetch_errors[tid] = (
                        "TRACE_NOT_FOUND"
                        if isinstance(error, RequestPairTraceMissing)
                        or getattr(error, "status_code", None) == 404
                        else "TRACE_FETCH_FAILED"
                    )
            available = fetched[tid] or ()
            retrieval = "FETCHED"
            deficits = (
                pair_deficits(log, available, target, query)
                if fetched[tid] is not None
                else (fetch_errors[tid],)
            )
        if not deficits:
            pair = tuple(s for s in available if s.span_id in {cid, pid})
            selected.update({(s.trace_id, s.span_id): s for s in pair})
            assessments.append(
                RequestPairAssessment(
                    source_log_id=log.evidence_id,
                    state="CORROBORATED",
                    evidence_ids=tuple(s.evidence_id for s in pair),
                    retrieval=retrieval,
                )
            )
        else:
            assessments.append(
                RequestPairAssessment(
                    source_log_id=log.evidence_id,
                    state="UNKNOWN",
                    deficits=deficits,
                    retrieval=retrieval,
                )
            )
    return RequestPairBatch(
        spans=tuple(selected.values()),
        assessments=tuple(assessments),
        attempted_trace_ids=tuple(fetched),
        returned_span_count=raw_count,
    )
