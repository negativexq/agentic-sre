"""Shared deterministic temporal semantics for production RCA."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from packages.rca.model import Finding, FindingKind


class TemporalContradictionCertainty(StrEnum):
    """Certainty that a finding is too late to be causal."""

    NOT_LATE = "NOT_LATE"
    DEFINITELY_LATE = "DEFINITELY_LATE"
    INTERVAL_UNCERTAIN = "INTERVAL_UNCERTAIN"
    UNKNOWN = "UNKNOWN"


OBJECT_CHANGE_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
    }
)


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def causal_time(finding: Finding) -> datetime | None:
    """Return the existing shared causal-time precedence used by verification."""
    raw = finding.details.get("initiating_at") or finding.details.get("schedule_active_from")
    return _parse_time(raw) or finding.at


def temporal_contradiction_certainty(
    finding: Finding,
    grace: timedelta,
) -> TemporalContradictionCertainty:
    """Classify whether the finding is certainly too late for its incident.

    Object changes are interval observations.  The previous observation is the
    lower bound, never an inferred exact change time.  Other evidence uses the
    established point-time causal timestamp.
    """
    onset = finding.incident_onset
    if onset is None:
        return TemporalContradictionCertainty.UNKNOWN
    boundary = onset + grace
    if finding.kind in OBJECT_CHANGE_KINDS and not (
        finding.details.get("initiating_at") or finding.details.get("schedule_active_from")
    ):
        current = finding.at
        previous = _parse_time(finding.details.get("previous_observed_at"))
        if current is None or previous is None:
            return TemporalContradictionCertainty.UNKNOWN
        if current <= boundary:
            return TemporalContradictionCertainty.NOT_LATE
        if previous > boundary:
            return TemporalContradictionCertainty.DEFINITELY_LATE
        return TemporalContradictionCertainty.INTERVAL_UNCERTAIN
    when = causal_time(finding)
    if when is None:
        return TemporalContradictionCertainty.UNKNOWN
    if when > boundary:
        return TemporalContradictionCertainty.DEFINITELY_LATE
    return TemporalContradictionCertainty.NOT_LATE


__all__ = [
    "OBJECT_CHANGE_KINDS",
    "TemporalContradictionCertainty",
    "causal_time",
    "temporal_contradiction_certainty",
]
