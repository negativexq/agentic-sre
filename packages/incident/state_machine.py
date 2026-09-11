"""Single deterministic authority for incident lifecycle transitions."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from packages.contracts import Incident, IncidentEvent, IncidentEventType, IncidentStatus

ALLOWED_TRANSITIONS: Mapping[IncidentStatus, frozenset[IncidentStatus]] = MappingProxyType(
    {
        IncidentStatus.OPEN: frozenset({IncidentStatus.TRIAGING}),
        IncidentStatus.TRIAGING: frozenset({IncidentStatus.INVESTIGATING}),
        IncidentStatus.INVESTIGATING: frozenset(
            {IncidentStatus.HYPOTHESIS_FORMED, IncidentStatus.ESCALATED, IncidentStatus.CLOSED}
        ),
        IncidentStatus.HYPOTHESIS_FORMED: frozenset(
            {IncidentStatus.VALIDATING, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.VALIDATING: frozenset(
            {IncidentStatus.REMEDIATION_PROPOSED, IncidentStatus.ESCALATED, IncidentStatus.CLOSED}
        ),
        IncidentStatus.REMEDIATION_PROPOSED: frozenset(
            {IncidentStatus.POLICY_EVALUATION, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.POLICY_EVALUATION: frozenset(
            {
                IncidentStatus.APPROVAL_REQUIRED,
                IncidentStatus.EXECUTING,
                IncidentStatus.ESCALATED,
                IncidentStatus.FAILED,
            }
        ),
        IncidentStatus.APPROVAL_REQUIRED: frozenset(
            {IncidentStatus.EXECUTING, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.EXECUTING: frozenset(
            {IncidentStatus.VERIFYING, IncidentStatus.FAILED, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.VERIFYING: frozenset(
            {IncidentStatus.RESOLVED, IncidentStatus.FAILED, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.RESOLVED: frozenset(),
        IncidentStatus.ESCALATED: frozenset(),
        IncidentStatus.FAILED: frozenset(),
        IncidentStatus.CLOSED: frozenset(),
    }
)


class InvalidTransitionError(ValueError):
    """Raised when a requested lifecycle transition is not allowed."""

    def __init__(self, source: IncidentStatus, target: IncidentStatus) -> None:
        self.source = source
        self.target = target
        super().__init__(f"transition from {source} to {target} is not allowed")


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """The new materialized state and its corresponding immutable event."""

    incident: Incident
    event: IncidentEvent


def transition(
    incident: Incident,
    target_status: IncidentStatus,
    reason: str,
    *,
    timestamp: datetime,
) -> TransitionResult:
    """Apply one legal transition without mutating the source incident."""
    if not reason.strip():
        raise ValueError("transition reason must not be empty")
    if target_status not in ALLOWED_TRANSITIONS[incident.status]:
        raise InvalidTransitionError(incident.status, target_status)

    updated_incident = incident.model_copy(
        update={"status": target_status, "updated_at": timestamp}
    )
    event = IncidentEvent(
        incident_id=incident.incident_id,
        event_type=IncidentEventType.STATE_CHANGED,
        timestamp=timestamp,
        correlation_id=incident.correlation_id,
        payload={
            "from_status": incident.status.value,
            "to_status": target_status.value,
            "reason": reason,
        },
    )
    return TransitionResult(incident=updated_incident, event=event)
