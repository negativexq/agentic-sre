"""Repositories with explicit transaction boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    ChangeRecord,
    ChangeType,
    Evidence,
    EvidenceSourceType,
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    TimeWindow,
)
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    EvidenceRow,
    IncidentEventRow,
    IncidentRow,
    ToolCallRow,
)

if TYPE_CHECKING:
    from packages.incident.state_machine import TransitionResult


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
        # Ensure the parent exists before the immutable event is flushed.
        self._session.flush()
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


class AlertRepository:
    """Read operations for normalized alerts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_incident(self, incident_id: object) -> list[Alert]:
        """Return alerts attached to an incident in stable start order."""
        rows = self._session.scalars(
            select(AlertRow)
            .where(AlertRow.incident_id == incident_id)
            .order_by(AlertRow.starts_at, AlertRow.alert_id)
        ).all()
        return [
            Alert(
                alert_id=row.alert_id,
                alert_name=row.alert_name,
                service=row.service,
                namespace=row.namespace,
                cluster=row.cluster,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                labels=row.labels,
                annotations=row.annotations,
                fingerprint=row.fingerprint,
                status=AlertStatus(row.status),
                source=AlertSource(row.source),
            )
            for row in rows
        ]


class ChangeRecordRepository:
    """Persistence operations for immutable historical change facts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, record: ChangeRecord) -> ChangeRecord:
        """Persist one harness/control-plane change fact."""
        self._session.add(
            ChangeRecordRow(
                change_id=record.change_id,
                timestamp=record.timestamp,
                resource_type=record.resource_type,
                resource_name=record.resource_name,
                change_type=record.change_type.value,
                before=record.before,
                after=record.after,
                revision=record.revision,
                source=record.source,
            )
        )
        self._session.commit()
        return record

    def between(
        self,
        *,
        resource_name: str,
        starts_at: datetime,
        ends_at: datetime,
        limit: int = 100,
    ) -> list[ChangeRecord]:
        """Read only records within the bounded incident observation window."""
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._session.scalars(
            select(ChangeRecordRow)
            .where(
                ChangeRecordRow.resource_name == resource_name,
                ChangeRecordRow.timestamp >= starts_at,
                ChangeRecordRow.timestamp <= ends_at,
            )
            .order_by(desc(ChangeRecordRow.timestamp), ChangeRecordRow.change_id)
            .limit(limit)
        ).all()
        return [
            ChangeRecord(
                change_id=row.change_id,
                timestamp=row.timestamp,
                resource_type=row.resource_type,
                resource_name=row.resource_name,
                change_type=ChangeType(row.change_type),
                before=row.before,
                after=row.after,
                revision=row.revision,
                source=row.source,
            )
            for row in rows
        ]


class EvidenceRepository:
    """Read operations for provenance-backed evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_incident(self, incident_id: object) -> list[Evidence]:
        """Return evidence ordered by collection time and identifier."""
        rows = self._session.scalars(
            select(EvidenceRow)
            .where(EvidenceRow.incident_id == incident_id)
            .order_by(EvidenceRow.collected_at, EvidenceRow.evidence_id)
        ).all()
        return [
            Evidence(
                evidence_id=row.evidence_id,
                incident_id=row.incident_id,
                source_type=EvidenceSourceType(row.source_type),
                source_system=row.source_system,
                observation=row.observation,
                time_window=TimeWindow.model_validate(row.time_window),
                tool_call_id=row.tool_call_id,
                raw_result_reference=row.raw_result_reference,
                collected_at=row.collected_at,
            )
            for row in rows
        ]


class ToolCallRepository:
    """Persistence operations for audited tool invocations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        tool_call_id: UUID,
        incident_id: UUID,
        tool_name: str,
        tool_version: str,
        request: dict[str, Any],
        response: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Append one immutable tool-call audit record."""
        self._session.add(
            ToolCallRow(
                tool_call_id=tool_call_id,
                incident_id=incident_id,
                tool_name=tool_name,
                tool_version=tool_version,
                request=request,
                response=response,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        self._session.commit()


class EvidenceWriteRepository:
    """Persistence boundary for provenance-validated evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, evidence: Evidence) -> Evidence:
        """Reject missing or cross-incident tool references before insert."""
        tool_call = self._session.get(ToolCallRow, evidence.tool_call_id)
        if tool_call is None or tool_call.incident_id != evidence.incident_id:
            raise ValueError("tool call does not belong to evidence incident")
        self._session.add(
            EvidenceRow(
                evidence_id=evidence.evidence_id,
                incident_id=evidence.incident_id,
                source_type=evidence.source_type.value,
                source_system=evidence.source_system,
                observation=evidence.observation,
                time_window=evidence.time_window.model_dump(mode="json"),
                tool_call_id=evidence.tool_call_id,
                raw_result_reference=evidence.raw_result_reference,
                collected_at=evidence.collected_at,
            )
        )
        self._session.commit()
        return evidence
