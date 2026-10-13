"""Read-only corroboration of explicit log identities against frozen spans.

This verifies export consistency, not source authenticity, causal origin or
manifest membership. It never queries a provider or changes a diagnosis.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.rca.model import LogRecord, TraceSpanObservation, TraceSpanStatus


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    incident_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    captured_at: datetime


class FrozenLog(Provenance):
    record: LogRecord


class FrozenSpan(Provenance):
    record: TraceSpanObservation


class FrozenRequestEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    incident_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    claim_id: str | None = None
    episode_start: datetime
    episode_end: datetime
    frozen_cutoff: datetime
    caller_service: str = Field(min_length=1)
    configured_peer: str = Field(min_length=1)
    logs: tuple[FrozenLog, ...] = Field(max_length=10000)
    spans: tuple[FrozenSpan, ...] = Field(max_length=10000)


class RequestBindingResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    log_evidence_id: str
    request_relation: Literal["CORROBORATED", "UNASSESSED"]
    deficits: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_references: tuple[str, ...]


class FrozenBindingReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    report_version: str = "frozen-request-binding.v1"
    authority: str = "EXPLANATORY_ONLY"
    verification_scope: str = "FROZEN_EXPORT_RECORD_CONSISTENCY"
    source_authenticity: str = "NOT_VERIFIED"
    manifest_membership: str = "NOT_VERIFIED_BY_FILE_ADAPTER"
    incident_id: str
    run_id: str
    claim_id: str | None
    relations: tuple[RequestBindingResult, ...]
    causal_origin: str = "UNASSESSED"
    same_fault_across_episodes: str = "UNASSESSED"
    recovery: str = "UNASSESSED"
    admission_change: bool = False


def _zoned(at: datetime) -> bool:
    return at.tzinfo is not None and at.utcoffset() is not None


def _identity(value: str | None, width: int) -> bool:
    return bool(value and re.fullmatch(rf"[0-9a-f]{{{width}}}", value) and int(value, 16))


def corroborate_frozen_requests(evidence: FrozenRequestEvidence) -> FrozenBindingReport:
    """Require unique CLIENT and direct SERVER parent, outcome and timing agreement.

    Request IDs are checked against both spans; they are never used to search
    for a missing span or override a mismatched trace/parent identity.
    """
    index: dict[tuple[str, str], list[FrozenSpan]] = defaultdict(list)
    for span in evidence.spans:
        index[(span.record.trace_id, span.record.span_id)].append(span)
    interval_valid = (
        all(
            _zoned(at)
            for at in (evidence.episode_start, evidence.episode_end, evidence.frozen_cutoff)
        )
        and evidence.episode_start <= evidence.episode_end <= evidence.frozen_cutoff
    )
    results = []
    for item in evidence.logs:
        log, attrs = item.record, item.record.attributes
        deficits: list[str] = []
        records: list[Provenance] = [item]
        ids = [log.evidence_id]
        if not interval_valid:
            deficits.append("INVALID_OR_UNZONED_EPISODE_BOUNDARY")
        if log.service != evidence.caller_service:
            deficits.append("LOG_CALLER_MISMATCH")
        if attrs.get("server.address") != evidence.configured_peer:
            deficits.append("LOG_CONFIGURED_PEER_MISSING_OR_MISMATCHED")
        if not attrs.get("request_id", "").strip():
            deficits.append("LOG_REQUEST_ID_MISSING")
        if not attrs.get("error.type"):
            deficits.append("LOG_FAILURE_TYPE_MISSING")
        trace_id, span_id, parent_id = (
            attrs.get("trace_id"),
            attrs.get("span_id"),
            attrs.get("parent_span_id"),
        )
        if not all((_identity(trace_id, 32), _identity(span_id, 16), _identity(parent_id, 16))):
            deficits.append("LOG_TRACE_SPAN_OR_PARENT_ID_MISSING_OR_INVALID")
        current = index.get((trace_id or "", span_id or ""), [])
        parents = index.get((trace_id or "", parent_id or ""), [])
        if len(current) != 1:
            deficits.append("CLIENT_SPAN_MISSING_OR_NON_UNIQUE")
        if len(parents) != 1:
            deficits.append("PARENT_SPAN_MISSING_OR_NON_UNIQUE")
        if len(current) == len(parents) == 1:
            client, parent = current[0].record, parents[0].record
            records.extend((current[0], parents[0]))
            ids.extend((client.evidence_id, parent.evidence_id))
            if len(set(ids)) != 3:
                deficits.append("LOG_AND_SPAN_EVIDENCE_NOT_DISTINCT")
            if client.span_kind not in {"CLIENT", "SPAN_KIND_CLIENT"} or parent.span_kind not in {
                "SERVER",
                "SPAN_KIND_SERVER",
            }:
                deficits.append("CLIENT_SERVER_KIND_NOT_RECORDED")
            if client.parent_span_id != parent.span_id or client.span_id == parent.span_id:
                deficits.append("DIRECT_PARENT_LINK_MISMATCH")
            if (
                client.service != evidence.caller_service
                or parent.service != evidence.caller_service
            ):
                deficits.append("SPAN_CALLER_MISMATCH")
            request_id = attrs.get("request_id")
            if not request_id or any(
                s.semantic_attributes.get("http.request.header.x_request_id") != request_id
                for s in (client, parent)
            ):
                deficits.append("SPAN_REQUEST_ID_MISSING_OR_MISMATCHED")
            if client.semantic_attributes.get("server.address") != evidence.configured_peer:
                deficits.append("CLIENT_CONFIGURED_PEER_MISSING_OR_MISMATCHED")
            if not attrs.get("error.type") or client.semantic_attributes.get(
                "error.type"
            ) != attrs.get("error.type"):
                deficits.append("CLIENT_FAILURE_TYPE_MISSING_OR_MISMATCHED")
            if client.status is not TraceSpanStatus.ERROR:
                deficits.append("CLIENT_ERROR_STATUS_NOT_RECORDED")
            status = parent.semantic_attributes.get("http.response.status_code", "")
            if not re.fullmatch(r"5[0-9]{2}", status):
                deficits.append("PARENT_5XX_NOT_RECORDED")
            if not attrs.get("http.request.method") or client.semantic_attributes.get(
                "http.request.method"
            ) != attrs.get("http.request.method"):
                deficits.append("CLIENT_HTTP_METHOD_MISSING_OR_MISMATCHED")
            times = (log.at, client.start_at, client.end_at, parent.start_at, parent.end_at)
            if any(at is None or not _zoned(at) for at in times):
                deficits.append("LOG_OR_SPAN_TIME_MISSING_OR_UNZONED")
            elif (
                interval_valid
                and log.at is not None
                and client.end_at is not None
                and parent.end_at is not None
            ):
                if (
                    not evidence.episode_start
                    <= parent.start_at
                    <= client.start_at
                    <= log.at
                    <= client.end_at
                    <= parent.end_at
                    <= evidence.episode_end
                ):
                    deficits.append("LOG_OR_SPAN_OUTSIDE_NESTED_EPISODE_INTERVAL")
        if len({r.source_reference for r in records}) != len(records):
            deficits.append("SOURCE_REFERENCES_NOT_DISTINCT")
        for record in records:
            if record.incident_id != evidence.incident_id or record.run_id != evidence.run_id:
                deficits.append("SOURCE_INCIDENT_OR_RUN_MISMATCH")
            if not _zoned(record.captured_at) or not _zoned(evidence.frozen_cutoff):
                deficits.append("SOURCE_CAPTURE_TIME_UNZONED")
            elif record.captured_at > evidence.frozen_cutoff:
                deficits.append("SOURCE_CAPTURE_AFTER_FROZEN_CUTOFF")
            event_end = (
                record.record.end_at
                if isinstance(record, FrozenSpan)
                else record.record.at
                if isinstance(record, FrozenLog)
                else None
            )
            if (
                event_end is not None
                and _zoned(event_end)
                and _zoned(record.captured_at)
                and record.captured_at < event_end
            ):
                deficits.append("SOURCE_CAPTURE_BEFORE_RECORDED_EVENT")
        results.append(
            RequestBindingResult(
                log_evidence_id=log.evidence_id,
                request_relation="UNASSESSED" if deficits else "CORROBORATED",
                deficits=tuple(sorted(set(deficits))),
                evidence_ids=tuple(ids),
                source_references=tuple(r.source_reference for r in records),
            )
        )
    return FrozenBindingReport(
        incident_id=evidence.incident_id,
        run_id=evidence.run_id,
        claim_id=evidence.claim_id,
        relations=tuple(results),
    )
