"""Repositories with explicit transaction boundaries."""

from collections.abc import Sequence

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from packages.contracts import (
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.incident import TransitionResult
from packages.storage.models import IncidentEventRow, IncidentRow


class IncidentNotFoundError(LookupError):
    """Raised when an incident is required but absent from storage."""


def _next_event_sequence(session: Session, incident_id: object) -> int:
    """Return the next monotonic sequence number for one incident."""
    latest = session.scalar(
        select(IncidentEventRow.sequence)
        .where(IncidentEventRow.incident_id == incident_id)
        .order_by(desc(IncidentEventRow.sequence))
        .limit(1)
    )
    return (latest or 0) + 1


def _incident_row_to_domain(row: IncidentRow) -> Incident:
    """Convert a persistence row into the public contract."""
    return Incident(
        incident_id=row.incident_id,
        status=IncidentStatus(row.status),
        severity=IncidentSeverity(row.severity),
        source=IncidentSource(row.source),
        title=row.title,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
        correlation_id=row.correlation_id,
    )


class IncidentRepository:
    """Persistence operations for current incident state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, incident: Incident) -> Incident:
        """Insert an incident and its creation event atomically."""
        row = IncidentRow(
            incident_id=incident.incident_id,
            status=incident.status.value,
            severity=incident.severity.value,
            source=incident.source.value,
            title=incident.title,
            description=incident.description,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
            correlation_id=incident.correlation_id,
        )
        event = IncidentEvent(
            incident_id=incident.incident_id,
            event_type=IncidentEventType.INCIDENT_CREATED,
            timestamp=incident.created_at,
            correlation_id=incident.correlation_id,
            payload={"status": incident.status.value},
        )
        self._session.add(row)
        self._session.add(
            IncidentEventRow(
                event_id=event.event_id,
                incident_id=event.incident_id,
                sequence=1,
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                payload=event.payload,
                correlation_id=event.correlation_id,
            )
        )
        self._session.commit()
        return incident

    def get(self, incident_id: object) -> Incident | None:
        """Load one incident by identifier."""
        row = self._session.get(IncidentRow, incident_id)
        return None if row is None else _incident_row_to_domain(row)

    def list(self) -> list[Incident]:
        """List incidents in stable creation order."""
        rows = self._session.scalars(
            select(IncidentRow).order_by(IncidentRow.created_at, IncidentRow.incident_id)
        ).all()
        return [_incident_row_to_domain(row) for row in rows]

    def save_transition(self, result: TransitionResult) -> Incident:
        """Persist state and event in one transaction."""
        row = self._session.get(IncidentRow, result.incident.incident_id)
        if row is None:
            raise IncidentNotFoundError(str(result.incident.incident_id))
        row.status = result.incident.status.value
        row.updated_at = result.incident.updated_at
        event = result.event
        self._session.add(
            IncidentEventRow(
                event_id=event.event_id,
                incident_id=event.incident_id,
                sequence=_next_event_sequence(self._session, event.incident_id),
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                payload=event.payload,
                correlation_id=event.correlation_id,
            )
        )
        self._session.commit()
        return result.incident


class IncidentEventRepository:
    """Read and append operations for immutable incident events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: IncidentEvent) -> IncidentEvent:
        """Append an event with a monotonic per-incident sequence."""
        self._session.add(
            IncidentEventRow(
                event_id=event.event_id,
                incident_id=event.incident_id,
                sequence=_next_event_sequence(self._session, event.incident_id),
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                payload=event.payload,
                correlation_id=event.correlation_id,
            )
        )
        self._session.commit()
        return event

    def list_for_incident(self, incident_id: object) -> list[IncidentEvent]:
        """Return a timeline ordered by its persisted sequence."""
        rows: Sequence[IncidentEventRow] = self._session.scalars(
            select(IncidentEventRow)
            .where(IncidentEventRow.incident_id == incident_id)
            .order_by(IncidentEventRow.sequence)
        ).all()
        return [
            IncidentEvent(
                event_id=row.event_id,
                incident_id=row.incident_id,
                event_type=IncidentEventType(row.event_type),
                timestamp=row.timestamp,
                payload=row.payload,
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]
