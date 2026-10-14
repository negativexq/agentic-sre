"""Bounded DEV request-pair retrieval from explicit log identities.

No searches, inferred identities, production capability registration or RCA authority.
The caller supplies a provider-boundary fetch or frozen-response reader.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from packages.rca.model import LogRecord, TraceSpanObservation


@dataclass(frozen=True)
class PairFetchResult:
    spans: tuple[TraceSpanObservation, ...]
    attempted_trace_ids: tuple[str, ...]
    returned_span_count: int
    deficits: tuple[tuple[str, str], ...]


def _identity(value: str | None, width: int) -> bool:
    return bool(value and re.fullmatch(rf"[0-9a-f]{{{width}}}", value) and int(value, 16))


def fetch_log_request_pairs(
    logs: Sequence[LogRecord],
    fetch: Callable[[str], tuple[TraceSpanObservation, ...]],
    *,
    max_trace_fetches: int = 8,
    max_selected_spans: int = 32,
) -> PairFetchResult:
    """Fetch each selected trace once; retain complete exact client/parent pairs.

    This is structural selection, not corroboration: outcomes, caller, peer,
    request identity, episode and provenance require the separate validator.
    Failed fetches consume budget. A pair is never split to fit the span cap.
    """
    if not 1 <= max_trace_fetches <= 8 or not 2 <= max_selected_spans <= 32:
        raise ValueError("DEV pair-read bounds: 1..8 trace fetches and 2..32 spans")
    fetched: dict[str, tuple[TraceSpanObservation, ...] | None] = {}
    selected: dict[tuple[str, str], TraceSpanObservation] = {}
    deficits: list[tuple[str, str]] = []
    raw_count = 0

    def order(log: LogRecord) -> tuple[float, str]:
        at = log.at
        return (
            at.timestamp() if at is not None and at.utcoffset() is not None else float("-inf"),
            log.evidence_id,
        )

    for log in sorted(logs, key=order):
        if log.at is None or log.at.utcoffset() is None:
            deficits.append((log.evidence_id, "LOG_EVENT_TIME_MISSING_OR_UNZONED"))
            continue
        tid, cid, pid = (log.attributes.get(k) for k in ("trace_id", "span_id", "parent_span_id"))
        if not (_identity(tid, 32) and _identity(cid, 16) and _identity(pid, 16)) or cid == pid:
            deficits.append((log.evidence_id, "EXPLICIT_TRACE_CLIENT_PARENT_ID_INVALID"))
            continue
        assert tid is not None and cid is not None and pid is not None
        # Do not fetch another whole trace when even one pair cannot fit.
        if len(selected) + 2 > max_selected_spans and not all(
            (tid, sid) in selected for sid in (cid, pid)
        ):
            deficits.append((log.evidence_id, "PAIR_SPAN_BUDGET_EXHAUSTED"))
            continue
        if tid not in fetched:
            if len(fetched) >= max_trace_fetches:
                deficits.append((log.evidence_id, "TRACE_FETCH_BUDGET_EXHAUSTED"))
                continue
            fetched[tid] = None
            try:
                fetched[tid] = fetch(tid)
                raw_count += len(fetched[tid] or ())
            except Exception:
                deficits.append((log.evidence_id, "TRACE_FETCH_FAILED"))
        payload = fetched[tid]
        if payload is None:
            deficits.append((log.evidence_id, "TRACE_RESPONSE_UNAVAILABLE"))
            continue
        client = [s for s in payload if s.trace_id == tid and s.span_id == cid]
        parent = [s for s in payload if s.trace_id == tid and s.span_id == pid]
        if len(client) != 1 or len(parent) != 1 or client[0].parent_span_id != pid:
            deficits.append((log.evidence_id, "EXACT_CLIENT_PARENT_PAIR_MISSING_OR_NON_UNIQUE"))
            continue
        selected[(tid, cid)] = client[0]
        selected[(tid, pid)] = parent[0]
    return PairFetchResult(tuple(selected.values()), tuple(fetched), raw_count, tuple(deficits))
