"""Deterministic incident lifecycle transition matrix tests."""

from datetime import UTC, datetime, timedelta

import pytest

from packages.contracts import (
    Incident,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.incident import ALLOWED_TRANSITIONS, InvalidTransitionError, transition

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def make_incident(status: IncidentStatus) -> Incident:
    return Incident(
        status=status,
        severity=IncidentSeverity.WARNING,
        source=IncidentSource.SYSTEM,
        title="State machine test incident",
        created_at=NOW,
        updated_at=NOW,
    )


def test_every_allowed_transition_updates_state_and_emits_event() -> None:
    timestamp = NOW + timedelta(minutes=1)

    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            incident = make_incident(source)
            result = transition(
                incident, target, "deterministic test transition", timestamp=timestamp
            )

            assert result.incident.incident_id == incident.incident_id
            assert result.incident.status is target
            assert result.incident.updated_at == timestamp
            assert incident.status is source
            assert result.event.incident_id == incident.incident_id
            assert result.event.event_type is IncidentEventType.STATE_CHANGED
            assert result.event.correlation_id == incident.correlation_id
            assert result.event.payload == {
                "from_status": source.value,
                "to_status": target.value,
                "reason": "deterministic test transition",
            }


def test_every_forbidden_transition_is_rejected() -> None:
    for source in IncidentStatus:
        for target in IncidentStatus:
            if target in ALLOWED_TRANSITIONS[source]:
                continue

            with pytest.raises(InvalidTransitionError):
                transition(make_incident(source), target, "forbidden transition", timestamp=NOW)


@pytest.mark.parametrize(
    "terminal_status",
    [
        IncidentStatus.RESOLVED,
        IncidentStatus.ESCALATED,
        IncidentStatus.FAILED,
        IncidentStatus.CLOSED,
    ],
)
def test_terminal_states_have_no_outgoing_transitions(terminal_status: IncidentStatus) -> None:
    assert ALLOWED_TRANSITIONS[terminal_status] == frozenset()


def test_empty_reason_is_rejected_before_transition() -> None:
    with pytest.raises(ValueError, match="reason"):
        transition(make_incident(IncidentStatus.OPEN), IncidentStatus.TRIAGING, "  ", timestamp=NOW)
