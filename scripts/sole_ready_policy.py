"""Frozen 2.11.0 predicate and additional denial reasons for the readiness audit."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from packages.rca.model import EntityRef, PodStatusObservation


def baseline_sole_ready(
    pod: EntityRef,
    others: Collection[EntityRef],
    statuses: Sequence[PodStatusObservation],
    start: datetime,
    end: datetime,
) -> bool:
    """The 2.11.0 implementation, frozen for reproducible paired recalculation."""
    if not any(status.pod == pod for status in statuses):
        return False
    for other in others:
        if other == pod:
            continue
        observations = [status for status in statuses if status.pod == other]
        if any(
            status.ready is True
            and status.observed_at >= start
            and (status.ready_since or status.observed_at) <= end
            for status in observations
        ):
            return False
        before = [status for status in observations if status.observed_at < start]
        if before and max(before, key=lambda status: status.observed_at).ready is True:
            return False
    return True


def readiness_gaps(
    pod: EntityRef,
    others: Collection[EntityRef],
    statuses: Sequence[PodStatusObservation],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    gaps = []
    for other in sorted(others, key=lambda p: p.canonical):
        if other == pod:
            continue
        observations = [s for s in statuses if s.pod == other]
        before = [s for s in observations if s.observed_at <= start]
        last_at = max((s.observed_at for s in before), default=None)
        latest = [s for s in before if s.observed_at == last_at]
        if not observations:
            reason = "MISSING_READINESS_RECORD"
        elif not latest:
            reason = "READINESS_AT_EXECUTION_START_UNKNOWN"
        elif any(s.ready is not False for s in latest):
            reason = "READINESS_AT_EXECUTION_START_NOT_KNOWN_FALSE"
        elif any(
            s.ready is None and s.observed_at >= start and (s.ready_since or s.observed_at) <= end
            for s in observations
        ):
            reason = "UNKNOWN_READINESS_OVERLAPS_EXECUTION"
        else:
            continue
        gaps.append(
            {
                "pod": other.canonical,
                "reason": reason,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "observations": [s.model_dump(mode="json") for s in observations],
            }
        )
    return gaps


def corrected_sole_ready(
    pod: EntityRef,
    others: Collection[EntityRef],
    statuses: Sequence[PodStatusObservation],
    start: datetime,
    end: datetime,
) -> bool:
    return baseline_sole_ready(pod, others, statuses, start, end) and not readiness_gaps(
        pod, others, statuses, start, end
    )
