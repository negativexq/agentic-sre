"""Select only visible, scoped source logs for the existing runtime-trace tool."""

from __future__ import annotations

from typing import cast

from packages.rca.claims import actor_findings
from packages.rca.engine import Case
from packages.rca.model import EntityRef, FindingKind, InformationGap, InvestigationQuery, LogRecord
from packages.rca.provider_adapter import RequestPairProviderBoundary
from packages.rca.trace_request_pairs import pair_deficits, valid_identity


def pair_provider(value: object | None) -> RequestPairProviderBoundary | None:
    pending = [value]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        adapter = getattr(current, "provider_adapter", None)
        if adapter is not None and adapter.supports("runtime_trace_pairs"):
            return cast(RequestPairProviderBoundary, adapter)
        pending.extend(getattr(current, name, None) for name in ("base", "_base", "full_source"))
    return None


def eligible_logs(
    case: Case,
    target: EntityRef,
    query: InvestigationQuery,
    *,
    missing_only: bool,
    gap: InformationGap | None = None,
) -> tuple[LogRecord, ...]:
    if target not in case.topology.latest or query.start is None or query.end is None:
        return ()
    cutoff = case.source.observation_cutoff()
    end = min(query.end, cutoff) if cutoff is not None else query.end
    names = case.topology.service_names(target)
    peers = {
        f.entity.name
        for h in case.hypotheses
        if gap is not None and h.hypothesis_id in gap.hypothesis_ids
        for f in actor_findings(h)
        if f.kind is FindingKind.DEPENDENCY_ERRORS and f.details.get("caller") == target.canonical
    }
    if gap is not None and gap.hypothesis_ids and not peers:
        return ()
    result = []
    for log in case.source.error_logs():
        attrs = log.attributes
        if peers and attrs.get("server.address") not in peers:
            continue
        if (
            log.service not in names
            or log.at is None
            or log.at.utcoffset() is None
            or not query.start <= log.at <= end
        ):
            continue
        if not all(
            attrs.get(name)
            for name in ("request_id", "server.address", "error.type", "http.request.method")
        ):
            continue
        if not (
            valid_identity(attrs.get("trace_id"), 32)
            and valid_identity(attrs.get("span_id"), 16)
            and valid_identity(attrs.get("parent_span_id"), 16)
        ):
            continue
        if missing_only and not pair_deficits(
            log, case.source.trace_observations(), target, query.model_copy(update={"end": end})
        ):
            continue
        result.append(log)
    return tuple(sorted(result, key=lambda r: (r.at, r.evidence_id)))
