"""Repositories with explicit transaction boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    ChangeRecord,
    ChangeScope,
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
from packages.rca.json_access import child, object_content_hash
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    DiagnosisRow,
    EvidenceRow,
    HypothesisRow,
    IncidentEventRow,
    IncidentRow,
    ObjectVersionRow,
    PolicyDecisionRow,
    RemediationProposalRow,
    ToolCallRow,
    VerificationResultRow,
)

if TYPE_CHECKING:
    from packages.incident.state_machine import TransitionResult


class IncidentNotFoundError(LookupError):
    """Raised when an incident is required but absent from storage."""


class BenchmarkStateRepository:
    """Narrow local-benchmark reset for incident and alert state only."""

    _INCIDENT_CHILD_TABLES = (
        IncidentEventRow,
        EvidenceRow,
        HypothesisRow,
        RemediationProposalRow,
        PolicyDecisionRow,
        VerificationResultRow,
        ToolCallRow,
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def reset_incident_alert_state(self) -> tuple[int, int]:
        """Delete only incident/alert state, preserving change history."""
        incident_count = self._session.scalar(select(func.count()).select_from(IncidentRow)) or 0
        alert_count = self._session.scalar(select(func.count()).select_from(AlertRow)) or 0
        for table in self._INCIDENT_CHILD_TABLES:
            self._session.execute(delete(table))
        self._session.execute(delete(AlertRow))
        self._session.execute(delete(IncidentRow))
        self._session.commit()
        return int(incident_count), int(alert_count)

    def incident_alert_counts(self) -> tuple[int, int]:
        """Return current incident and alert counts for contamination checks."""
        incidents = self._session.scalar(select(func.count()).select_from(IncidentRow)) or 0
        alerts = self._session.scalar(select(func.count()).select_from(AlertRow)) or 0
        return int(incidents), int(alerts)


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
                scope=record.scope.value,
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
        scope: ChangeScope | None = None,
        limit: int = 100,
    ) -> list[ChangeRecord]:
        """Read only records within the bounded incident observation window."""
        if limit < 1:
            raise ValueError("limit must be positive")
        filters = [
            ChangeRecordRow.resource_name == resource_name,
            ChangeRecordRow.timestamp >= starts_at,
            ChangeRecordRow.timestamp <= ends_at,
        ]
        if scope is not None:
            filters.append(ChangeRecordRow.scope == scope.value)
        rows = self._session.scalars(
            select(ChangeRecordRow)
            .where(*filters)
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
                scope=ChangeScope(row.scope),
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


class ObjectVersionRepository:
    """Append-only journal of full object bodies, deduplicated by content."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def content_hash(body: dict[str, Any]) -> str:
        return object_content_hash(body)

    def record(self, body: dict[str, Any], observed_at: datetime) -> bool:
        """Store ``body`` unless the object's latest stored version has the same content."""
        metadata = child(body, "metadata")
        kind, name = body.get("kind"), metadata.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            raise ValueError("object body needs kind and metadata.name")
        namespace = str(metadata.get("namespace") or "_cluster")
        key = f"{namespace}/{kind}/{name}"
        digest = self.content_hash(body)
        latest = self._session.scalars(
            select(ObjectVersionRow.content_hash)
            .where(ObjectVersionRow.object_key == key)
            .order_by(desc(ObjectVersionRow.observed_at), desc(ObjectVersionRow.version_id))
            .limit(1)
        ).first()
        if latest == digest:
            return False
        self._session.add(
            ObjectVersionRow(
                object_key=key,
                namespace=namespace,
                kind=kind,
                name=name,
                observed_at=observed_at,
                content_hash=digest,
                body=body,
            )
        )
        self._session.commit()
        return True

    def history(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[tuple[str, datetime, dict[str, Any], int]]:
        """Versions in the window plus each object's last version before it, oldest first."""
        rows = self._session.scalars(
            select(ObjectVersionRow)
            .where(
                ObjectVersionRow.namespace.in_(namespaces | {"_cluster"}),
                ObjectVersionRow.observed_at <= ends_at,
            )
            .order_by(ObjectVersionRow.observed_at, ObjectVersionRow.version_id)
        ).all()
        result: list[tuple[str, datetime, dict[str, Any], int]] = []
        baseline: dict[str, ObjectVersionRow] = {}
        for row in rows:
            if row.observed_at < starts_at:
                baseline[row.object_key] = row
            else:
                result.append((row.object_key, row.observed_at, row.body, row.version_id))
        before = [
            (row.object_key, row.observed_at, row.body, row.version_id) for row in baseline.values()
        ]
        return sorted([*before, *result], key=lambda item: (item[1], item[3]))


class DiagnosisRepository:
    """Stored diagnoses per incident."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, incident_id: object, document: dict[str, Any], created_at: datetime) -> None:
        self._session.add(
            DiagnosisRow(
                incident_id=incident_id,
                created_at=created_at,
                root_cause=document.get("root_cause") and _canonical(document["root_cause"]),
                confidence=str(document.get("confidence")),
                mode=str(document.get("mode")),
                document=document,
            )
        )
        self._session.commit()

    def latest(self, incident_id: object) -> dict[str, Any] | None:
        row = self._session.scalars(
            select(DiagnosisRow)
            .where(DiagnosisRow.incident_id == incident_id)
            .order_by(desc(DiagnosisRow.created_at), desc(DiagnosisRow.diagnosis_id))
            .limit(1)
        ).first()
        return dict(row.document) if row is not None else None

    def summaries(self) -> dict[str, dict[str, Any]]:
        """Latest root cause and confidence per incident id."""
        result: dict[str, dict[str, Any]] = {}
        for row in self._session.scalars(
            select(DiagnosisRow).order_by(DiagnosisRow.created_at, DiagnosisRow.diagnosis_id)
        ).all():
            result[str(row.incident_id)] = {
                "root_cause": row.root_cause,
                "confidence": row.confidence,
                "created_at": row.created_at,
            }
        return result


def _canonical(value: Any) -> str:
    if isinstance(value, dict):
        return f"{value.get('namespace')}/{value.get('kind')}/{value.get('name')}"
    return str(value)
