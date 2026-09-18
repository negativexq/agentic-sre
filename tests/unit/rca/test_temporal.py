"""Production shared temporal-semantic regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.model import EntityRef, Finding, FindingKind
from packages.rca.temporal import (
    TemporalContradictionCertainty,
    causal_time,
    temporal_contradiction_certainty,
)

ONSET = datetime(2026, 1, 1, tzinfo=UTC)
GRACE = timedelta(minutes=15)
ENTITY = EntityRef(namespace="shop", kind="Deployment", name="checkout")


def _finding(
    kind: FindingKind,
    at: datetime,
    *,
    previous: datetime | None = None,
    details: dict[str, object] | None = None,
) -> Finding:
    finding_details = dict(details or {})
    if previous is not None:
        finding_details["previous_observed_at"] = previous.isoformat()
    return Finding(
        kind=kind,
        entity=ENTITY,
        at=at,
        incident_onset=ONSET,
        summary=f"{kind.value} on {ENTITY.canonical}",
        evidence_ids=("evidence",),
        details=finding_details,
    )


def test_object_change_temporal_certainty_boundaries() -> None:
    assert (
        temporal_contradiction_certainty(
            _finding(
                FindingKind.CONFIG_CHANGE,
                ONSET + timedelta(minutes=10),
                previous=ONSET + timedelta(minutes=1),
            ),
            GRACE,
        )
        is TemporalContradictionCertainty.NOT_LATE
    )
    assert (
        temporal_contradiction_certainty(
            _finding(
                FindingKind.CONFIG_CHANGE,
                ONSET + timedelta(minutes=30),
                previous=ONSET + timedelta(minutes=20),
            ),
            GRACE,
        )
        is TemporalContradictionCertainty.DEFINITELY_LATE
    )
    assert (
        temporal_contradiction_certainty(
            _finding(
                FindingKind.CONFIG_CHANGE,
                ONSET + timedelta(minutes=30),
                previous=ONSET + timedelta(minutes=10),
            ),
            GRACE,
        )
        is TemporalContradictionCertainty.INTERVAL_UNCERTAIN
    )


def test_missing_previous_observation_is_unknown() -> None:
    finding = _finding(FindingKind.SPEC_CHANGE, ONSET + timedelta(minutes=30))
    assert (
        temporal_contradiction_certainty(finding, GRACE) is TemporalContradictionCertainty.UNKNOWN
    )


def test_explicit_causal_time_precedence_is_shared() -> None:
    early = ONSET + timedelta(minutes=10)
    late = ONSET + timedelta(minutes=30)
    initiating = _finding(
        FindingKind.FAULT_INJECTION,
        late,
        details={"initiating_at": early.isoformat()},
    )
    scheduled = _finding(
        FindingKind.FAULT_INJECTION,
        late,
        details={"schedule_active_from": early.isoformat()},
    )
    assert causal_time(initiating) == early
    assert causal_time(scheduled) == early
    assert (
        temporal_contradiction_certainty(initiating, GRACE)
        is TemporalContradictionCertainty.NOT_LATE
    )
    assert (
        temporal_contradiction_certainty(scheduled, GRACE)
        is TemporalContradictionCertainty.NOT_LATE
    )


def test_late_point_time_remains_hard() -> None:
    late_event = _finding(FindingKind.FAILURE_EVENT, ONSET + timedelta(minutes=30))
    assert (
        temporal_contradiction_certainty(late_event, GRACE)
        is TemporalContradictionCertainty.DEFINITELY_LATE
    )
