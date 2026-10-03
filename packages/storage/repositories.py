"""Repositories with explicit transaction boundaries."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

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
from packages.rca.model import (
    CLUSTER_SCOPE,
    Diagnosis,
    JournalEntry,
    Lifecycle,
    LogRecord,
    PreconditionStatus,
    RequirementAuditReason,
    RequirementEvaluation,
    Symptoms,
    TraceSpanObservation,
    object_key,
    snapshot_evidence_id,
)
from packages.rca.requirements import requirement_key, requirement_targets_document
from packages.storage.models import (
    DIAGNOSIS_TRIGGERS,
    LIFECYCLE_OBSERVATION_TYPES,
    AlertCoveragePollRow,
    AlertCoverageSegmentRow,
    AlertRow,
    ChangeRecordRow,
    ChangeStreamGapRow,
    DiagnosisRow,
    EmailDeliveryRow,
    EntityInstanceRow,
    EventVersionRow,
    EvidenceRequirementRow,
    EvidenceRow,
    IncidentEventRow,
    IncidentRow,
    InvestigationReadRow,
    InvestigationRunRow,
    JournalArrivalRow,
    LifecycleObservationRow,
    LogObservationRow,
    ObjectVersionRow,
    ReportRow,
    SnapshotCycleObjectRow,
    SnapshotCycleRow,
    StreamFollowRow,
    TraceCaptureRow,
    TraceObservationRow,
    UTCDateTime,
)

if TYPE_CHECKING:
    from packages.incident.state_machine import TransitionResult
    from packages.rca.alert_coverage import AlertCoverageBoundary, AlertCoverageConfig


class AlertCoverageRepository:
    """Alert-channel coverage segments and their poll audit (M21 contract §10.2)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def open_segment(self, source: str) -> AlertCoverageSegmentRow | None:
        return self._session.scalar(
            select(AlertCoverageSegmentRow)
            .where(AlertCoverageSegmentRow.source == source)
            .where(AlertCoverageSegmentRow.status == "OPEN")
            .order_by(desc(AlertCoverageSegmentRow.segment_id))
            .limit(1)
        )

    def record_success(
        self,
        *,
        source: str,
        attempted_at: datetime,
        completed_at: datetime,
        active_alerts: int,
        config: AlertCoverageConfig,
    ) -> int:
        """Extend the open segment, or break it after a gap and start a new one."""
        segment = self.open_segment(source)
        if segment is not None and config.continues(segment.last_success_at, completed_at):
            segment.last_success_at = completed_at
        else:
            if segment is not None:
                segment.status = "BROKEN_GAP"
                segment.ended_at = segment.last_success_at
            segment = AlertCoverageSegmentRow(
                source=source,
                started_at=completed_at,
                last_success_at=completed_at,
                ended_at=None,
                status="OPEN",
            )
            self._session.add(segment)
            self._session.flush()
        self._session.add(
            AlertCoveragePollRow(
                source=source,
                segment_id=segment.segment_id,
                attempted_at=attempted_at,
                completed_at=completed_at,
                success=True,
                error_type=None,
                active_alerts=active_alerts,
            )
        )
        self._session.commit()
        return segment.segment_id

    def boundary_at(
        self, *, source: str, at: datetime, config: AlertCoverageConfig
    ) -> AlertCoverageBoundary:
        """The contiguous coverage segment a run boundary at ``at`` lies in, if any.

        The latest segment started at or before ``at`` covers it when ``at`` is
        within its observed span, or when it is still open and ``at`` is within the
        polling-gap tolerance of its last success. Otherwise coverage is unavailable.
        """
        from packages.rca.alert_coverage import (
            COVERAGE_CONTIGUOUS,
            UNAVAILABLE_COVERAGE,
            AlertCoverageBoundary,
        )

        segment = self._session.scalar(
            select(AlertCoverageSegmentRow)
            .where(AlertCoverageSegmentRow.source == source)
            .where(AlertCoverageSegmentRow.started_at <= at)
            .order_by(
                desc(AlertCoverageSegmentRow.started_at), desc(AlertCoverageSegmentRow.segment_id)
            )
            .limit(1)
        )
        if segment is None:
            return UNAVAILABLE_COVERAGE
        within = at <= segment.last_success_at
        live = segment.status == "OPEN" and config.continues(segment.last_success_at, at)
        if not (within or live):
            return UNAVAILABLE_COVERAGE
        return AlertCoverageBoundary(
            status=COVERAGE_CONTIGUOUS,
            segment_id=segment.segment_id,
            observation_start=segment.started_at,
            last_success=segment.last_success_at,
        )

    def record_failure(
        self, *, source: str, attempted_at: datetime, completed_at: datetime, error_type: str
    ) -> None:
        """An explicit failure ends coverage at the last success; no tolerance hides it."""
        segment = self.open_segment(source)
        if segment is not None:
            segment.status = "CLOSED_FAILURE"
            segment.ended_at = segment.last_success_at
        self._session.add(
            AlertCoveragePollRow(
                source=source,
                segment_id=None,
                attempted_at=attempted_at,
                completed_at=completed_at,
                success=False,
                error_type=error_type,
                active_alerts=None,
            )
        )
        self._session.commit()


class RequirementOnsetUnavailable(RuntimeError):
    """A requirement's opening revision does not carry a readable onset."""


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

    def create(self, incident: Incident, *, commit: bool = True) -> Incident:
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
        if commit:
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

    def append(self, event: IncidentEvent, *, commit: bool = True) -> IncidentEvent:
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
        if commit:
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

    def ids_for_incident(self, incident_id: object) -> list[object]:
        """Exact ids of the alerts attached to an incident."""
        return list(
            self._session.scalars(
                select(AlertRow.alert_id).where(AlertRow.incident_id == incident_id)
            ).all()
        )

    def list_for_incident(self, incident_id: object) -> list[Alert]:
        """Return alerts attached to an incident in stable start order."""
        return self._alerts(AlertRow.incident_id == incident_id)

    def _alerts(self, condition: Any) -> list[Alert]:
        rows = self._session.scalars(
            select(AlertRow).where(condition).order_by(AlertRow.starts_at, AlertRow.alert_id)
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

    def recent(
        self,
        *,
        limit: int = 100,
        scope: ChangeScope | None = None,
        change_type: ChangeType | None = None,
        resource_query: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> list[ChangeRecord]:
        """List recent change facts across resources for the changes explorer.

        This is a read-only, bounded view for the UI; investigation backends
        still use the tighter :meth:`between` window keyed to one resource.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        filters = []
        if scope is not None:
            filters.append(ChangeRecordRow.scope == scope.value)
        if change_type is not None:
            filters.append(ChangeRecordRow.change_type == change_type.value)
        if resource_query:
            filters.append(ChangeRecordRow.resource_name.ilike(f"%{resource_query}%"))
        if starts_at is not None:
            filters.append(ChangeRecordRow.timestamp >= starts_at)
        if ends_at is not None:
            filters.append(ChangeRecordRow.timestamp <= ends_at)
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
                # Stored as a JSON dict of ISO strings; coerce on read rather
                # than requiring datetime instances the JSON column cannot hold.
                time_window=TimeWindow.model_validate(row.time_window, strict=False),
                tool_call_id=row.tool_call_id,
                raw_result_reference=row.raw_result_reference,
                collected_at=row.collected_at,
            )
            for row in rows
        ]


class TraceObservationRepository:
    """Append-only spans captured for an incident, frozen by a run's manifest (live-trace-design.md §3)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self, incident_id: object, spans: Sequence[TraceSpanObservation], observed_at: datetime
    ) -> int:
        """Persist the spans not yet held for this incident; return how many were added."""
        stored = 0
        for span in spans:
            key = hashlib.sha256(f"{span.trace_id}|{span.span_id}".encode()).hexdigest()
            exists = self._session.scalar(
                select(TraceObservationRow.observation_id).where(
                    TraceObservationRow.incident_id == incident_id,
                    TraceObservationRow.dedup_key == key,
                )
            )
            if exists is not None:
                continue
            self._session.add(
                TraceObservationRow(
                    incident_id=incident_id,
                    service=span.service,
                    event_at=span.start_at,
                    observed_at=observed_at,
                    span=span.model_dump(mode="json"),
                    dedup_key=key,
                )
            )
            stored += 1
        self._session.flush()
        return stored

    def incident_observation_ids(
        self, *, incident_id: object, starts_at: datetime, ends_at: datetime
    ) -> list[int]:
        """Spans of the incident that began inside the window and were captured by its end."""
        return list(
            self._session.scalars(
                select(TraceObservationRow.observation_id)
                .where(
                    TraceObservationRow.incident_id == incident_id,
                    TraceObservationRow.event_at >= starts_at,
                    TraceObservationRow.event_at <= ends_at,
                    TraceObservationRow.observed_at <= ends_at,
                )
                .order_by(TraceObservationRow.event_at, TraceObservationRow.observation_id)
            ).all()
        )

    def spans(self, observation_ids: Sequence[int]) -> list[TraceSpanObservation]:
        """Exactly these spans, in start order."""
        if not observation_ids:
            return []
        rows = self._session.scalars(
            select(TraceObservationRow)
            .where(TraceObservationRow.observation_id.in_(observation_ids))
            .order_by(TraceObservationRow.event_at, TraceObservationRow.observation_id)
        ).all()
        return [TraceSpanObservation.model_validate(row.span) for row in rows]


class TraceCaptureRepository:
    """How complete each service's trace read was (live-trace-design.md §3.2)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, incident_id: object, reads: Sequence[Any], captured_at: datetime) -> None:
        for read in reads:
            self._session.add(
                TraceCaptureRow(
                    incident_id=incident_id,
                    namespace=read.namespace,
                    service=read.service,
                    starts_at=read.starts_at,
                    ends_at=read.ends_at,
                    completeness=read.completeness,
                    spans=read.spans,
                    error=read.error[:255],
                    captured_at=captured_at,
                )
            )
        self._session.flush()


class LogObservationRepository:
    """Append-only bounded log observations used by diagnosis replay."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _dedup_key(record: LogRecord) -> str:
        value = json.dumps(
            {
                "service": record.service,
                "at": record.at.isoformat() if record.at is not None else None,
                "severity": record.severity,
                "message": record.message,
                "evidence_id": record.evidence_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def record(
        self,
        incident_id: object,
        records: Sequence[LogRecord],
        observed_at: datetime,
        *,
        source_system: str = "loki",
        source_read_ids: Sequence[int | None] | None = None,
    ) -> int:
        """Persist new records and return the number added."""
        if source_read_ids is not None and len(source_read_ids) != len(records):
            raise ValueError("source_read_ids must align one-for-one with records")
        stored = 0
        for index, record in enumerate(records):
            dedup_key = self._dedup_key(record)
            exists = self._session.scalar(
                select(LogObservationRow.observation_id).where(
                    LogObservationRow.incident_id == incident_id,
                    LogObservationRow.dedup_key == dedup_key,
                )
            )
            if exists is not None:
                continue
            self._session.add(
                LogObservationRow(
                    incident_id=incident_id,
                    service=record.service,
                    event_at=record.at,
                    observed_at=observed_at,
                    severity=record.severity,
                    message=record.message[:4000],
                    evidence_id=record.evidence_id[:512],
                    dedup_key=dedup_key,
                    source_system=source_system,
                    source_read_id=(
                        source_read_ids[index] if source_read_ids is not None else None
                    ),
                )
            )
            stored += 1
        if stored:
            self._session.commit()
        return stored

    def list_for_incident(
        self, *, incident_id: object, starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord]:
        """Return observations visible in an incident's frozen window."""
        return [
            _log_record(row)
            for row in self._incident_rows(
                incident_id=incident_id, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def incident_observation_ids(
        self, *, incident_id: object, starts_at: datetime, ends_at: datetime
    ) -> list[int]:
        """Exact ids of the observations ``list_for_incident`` returns for this window."""
        return [
            row.observation_id
            for row in self._incident_rows(
                incident_id=incident_id, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def records(self, observation_ids: Sequence[int]) -> list[LogRecord]:
        """Exactly these observations, in observation order."""
        if not observation_ids:
            return []
        rows = self._session.scalars(
            select(LogObservationRow)
            .where(LogObservationRow.observation_id.in_(observation_ids))
            .order_by(LogObservationRow.observed_at, LogObservationRow.observation_id)
        ).all()
        return [_log_record(row) for row in rows]

    def _incident_rows(
        self, *, incident_id: object, starts_at: datetime, ends_at: datetime
    ) -> list[LogObservationRow]:
        rows = self._session.scalars(
            select(LogObservationRow)
            .where(
                LogObservationRow.incident_id == incident_id,
                LogObservationRow.observed_at <= ends_at,
                or_(
                    LogObservationRow.event_at.is_(None),
                    LogObservationRow.event_at <= ends_at,
                ),
                or_(
                    LogObservationRow.event_at >= starts_at,
                    LogObservationRow.event_at.is_(None),
                    LogObservationRow.observed_at >= starts_at,
                ),
            )
            .order_by(LogObservationRow.observed_at, LogObservationRow.observation_id)
        ).all()
        return list(rows)


class InvestigationReadRepository:
    """Append one complete successful provider-read envelope and commit it."""

    _APPEND_ATTEMPTS = 5

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_run(self, run_id: str) -> list[InvestigationReadRow]:
        """Reload exactly one run's provider tape in authoritative order."""
        return list(
            self._session.scalars(
                select(InvestigationReadRow)
                .where(InvestigationReadRow.run_id == run_id)
                .order_by(InvestigationReadRow.sequence.asc())
            ).all()
        )

    def _next_sequence(self, run_id: str) -> int:
        latest = self._session.scalar(
            select(InvestigationReadRow.sequence)
            .where(InvestigationReadRow.run_id == run_id)
            .order_by(desc(InvestigationReadRow.sequence))
            .limit(1)
        )
        return (latest or 0) + 1

    def append_success(
        self,
        *,
        run_id: str,
        caller_class: str,
        capability: str,
        query_key: str,
        query_descriptor: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        observation: Any,
        evidence_ids: Sequence[str],
    ) -> int:
        """Insert and commit one SUCCESS row; sequence is serialized per run."""
        return self._append(
            run_id=run_id,
            caller_class=caller_class,
            capability=capability,
            query_key=query_key,
            query_descriptor=query_descriptor,
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            observation=observation,
            evidence_ids=evidence_ids,
            error_type=None,
            error_message=None,
        )

    def append_error(
        self,
        *,
        run_id: str,
        caller_class: str,
        capability: str,
        query_key: str,
        query_descriptor: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        error_type: str,
        error_message: str,
    ) -> int:
        """Insert and commit one complete ERROR row; never mutate a prior read."""
        return self._append(
            run_id=run_id,
            caller_class=caller_class,
            capability=capability,
            query_key=query_key,
            query_descriptor=query_descriptor,
            started_at=started_at,
            finished_at=finished_at,
            status="ERROR",
            observation=None,
            evidence_ids=(),
            error_type=error_type,
            error_message=error_message,
        )

    def _append(
        self,
        *,
        run_id: str,
        caller_class: str,
        capability: str,
        query_key: str,
        query_descriptor: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        status: str,
        observation: Any,
        evidence_ids: Sequence[str],
        error_type: str | None,
        error_message: str | None,
    ) -> int:
        for attempt in range(self._APPEND_ATTEMPTS):
            if self._session.get_bind().dialect.name == "postgresql":
                # Transaction-scoped lock serializes max+1 allocation for this
                # run. hashtext collisions only serialize unrelated runs.
                self._session.execute(select(func.pg_advisory_xact_lock(func.hashtext(run_id))))
            sequence = self._next_sequence(run_id)
            row = InvestigationReadRow(
                run_id=run_id,
                sequence=sequence,
                caller_class=caller_class,
                capability=capability,
                query_key=query_key,
                query_descriptor=query_descriptor,
                started_at=started_at,
                finished_at=finished_at,
                committed_at=datetime.now(UTC),
                status=status,
                observation=observation,
                evidence_ids=list(evidence_ids),
                error_type=error_type,
                error_message=error_message,
            )
            self._session.add(row)
            try:
                self._session.flush()
                read_id = row.read_id
                assert read_id is not None
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                if attempt + 1 == self._APPEND_ATTEMPTS:
                    raise
                # Retry only a run-sequence collision. Other integrity errors
                # are real persistence failures and must reach the caller.
                collision = self._session.scalar(
                    select(InvestigationReadRow.read_id).where(
                        InvestigationReadRow.run_id == run_id,
                        InvestigationReadRow.sequence == sequence,
                    )
                )
                if collision is None:
                    raise
                continue
            return read_id
        raise RuntimeError(
            f"could not allocate an investigation-read sequence for {run_id} "
            f"after {self._APPEND_ATTEMPTS} attempts"
        )


def _log_record(row: LogObservationRow) -> LogRecord:
    return LogRecord(
        service=row.service,
        at=row.event_at,
        severity=row.severity,
        message=row.message,
        evidence_id=row.evidence_id,
    )


def _event_body(row: EventVersionRow) -> dict[str, Any]:
    return _body_with_persisted_uid(row.body, "involvedObject", row.involved_uid)


def event_identity(body: dict[str, Any], namespace: str) -> str:
    """Return a stable logical identity, separate from an observed version key.

    Kubernetes ``metadata.uid`` is the preferred identity.  Older or synthetic
    Event payloads may omit it, so the fallback uses stable object/source
    fields and deliberately excludes mutable ``count`` and timestamp fields.
    """
    metadata = child(body, "metadata")
    uid = metadata.get("uid")
    if isinstance(uid, str) and uid:
        return f"uid:{namespace}:{uid}"
    involved = child(body, "involvedObject")
    source = body.get("source")
    source_component = source.get("component") if isinstance(source, dict) else None
    stable = {
        "namespace": namespace,
        "involved_uid": involved.get("uid"),
        "involved_kind": involved.get("kind"),
        "involved_name": involved.get("name"),
        "reason": body.get("reason"),
        "reporting_component": body.get("reportingComponent") or source_component,
        "event_name": metadata.get("name"),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return f"fallback:{hashlib.sha256(encoded.encode()).hexdigest()}"


def event_version_key(identity: str, body: dict[str, Any]) -> str:
    """Hash the stable mutable Event state to key an append-only observation."""
    metadata = child(body, "metadata")
    involved = child(body, "involvedObject")
    source = body.get("source")
    stable_state = {
        "identity": identity,
        "count": body.get("count"),
        "firstTimestamp": body.get("firstTimestamp"),
        "lastTimestamp": body.get("lastTimestamp"),
        "eventTime": body.get("eventTime"),
        "reason": body.get("reason"),
        "type": body.get("type"),
        "message": body.get("message"),
        "involvedObject": {
            "uid": involved.get("uid"),
            "kind": involved.get("kind"),
            "name": involved.get("name"),
        },
        "reportingComponent": body.get("reportingComponent"),
        "source": source,
        "event_name": metadata.get("name"),
    }
    encoded = json.dumps(stable_state, sort_keys=True, separators=(",", ":"), default=str)
    return f"{identity}|{hashlib.sha256(encoded.encode()).hexdigest()}"


def _body_with_persisted_uid(
    body: dict[str, Any], parent_key: str, uid: str | None
) -> dict[str, Any]:
    """Expose the UID column through the existing body-based read contract."""
    result = dict(body)
    parent = body.get(parent_key)
    if isinstance(parent, dict):
        nested = dict(parent)
    elif uid is not None:
        nested = {}
    else:
        return result
    if uid is None:
        nested.pop("uid", None)
    else:
        nested["uid"] = uid
    result[parent_key] = nested
    return result


def _nested_uid(body: dict[str, Any], parent_key: str) -> str | None:
    uid = child(body, parent_key).get("uid")
    return uid if isinstance(uid, str) else None


def _arrival(journal: str, row_type: Any) -> tuple[Any, Any]:
    """An outer-join target and a row's effective observation time (late-evidence-design.md §3).

    The Connector's observation when the row arrived through its stream, otherwise the control plane's
    arrival (``observed_at``). It is never later than the arrival, and never earlier than an actual
    observation, so it admits no hindsight.
    """
    arrival = aliased(JournalArrivalRow)
    on = and_(arrival.journal == journal, arrival.version_id == row_type.version_id)
    effective = func.coalesce(
        arrival.connector_observed_at, row_type.observed_at, type_=UTCDateTime()
    )
    return (arrival, on), effective


def _record_arrival(
    session: Session, journal: str, row: EventVersionRow | ObjectVersionRow, at: datetime | None
) -> None:
    """Keep when the Connector observed a new journal row (contract §15, measurement only)."""
    if at is None:
        return
    session.flush()
    session.add(
        JournalArrivalRow(journal=journal, version_id=row.version_id, connector_observed_at=at)
    )


class ChangeStreamGapRepository:
    """Persisted losses of change-stream continuity (late-evidence-design.md §4.2); append-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        reason: str,
        at: datetime,
        *,
        since: datetime | None,
        scope: tuple[str, str] | None,
    ) -> None:
        namespace, kind = scope if scope is not None else (None, None)
        known = self._session.scalar(
            select(ChangeStreamGapRow.gap_id).where(
                ChangeStreamGapRow.reason == reason,
                ChangeStreamGapRow.at == at,
                ChangeStreamGapRow.namespace.is_(None)
                if namespace is None
                else ChangeStreamGapRow.namespace == namespace,
                ChangeStreamGapRow.kind.is_(None)
                if kind is None
                else ChangeStreamGapRow.kind == kind,
            )
        )
        if known is not None:
            return  # read again by a restarted control plane from the Connector's buffer
        self._session.add(
            ChangeStreamGapRow(reason=reason, namespace=namespace, kind=kind, since=since, at=at)
        )
        self._session.commit()

    def overlapping(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[ChangeStreamGapRow]:
        """The gaps of these namespaces' scopes, and every global gap, that overlap the window.

        A gap with no known start overlaps every window that does not end before the gap does.
        """
        return list(
            self._session.scalars(
                select(ChangeStreamGapRow)
                .where(
                    ChangeStreamGapRow.at >= starts_at,
                    or_(ChangeStreamGapRow.since.is_(None), ChangeStreamGapRow.since <= ends_at),
                    or_(
                        ChangeStreamGapRow.namespace.is_(None),
                        ChangeStreamGapRow.namespace.in_(sorted(namespaces)),
                    ),
                )
                .order_by(ChangeStreamGapRow.at, ChangeStreamGapRow.gap_id)
            )
        )


class StreamFollowRepository:
    """How far each control-plane process followed each Connector run's change stream."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def follow(
        self, segment_id: int | None, *, epoch: str, first_seq: int, last_seq: int, since: datetime
    ) -> tuple[int, datetime]:
        """Advance this process's segment, or start one; its id and the instant it is followed since.

        A new segment keeps the ``followed_since`` of the latest earlier segment of the same epoch when
        it starts at most one item past where that one stopped: nothing between them went unread.
        """
        if segment_id is not None:
            row = self._session.get(StreamFollowRow, segment_id)
            if row is not None and row.epoch == epoch:
                row.last_seq = max(row.last_seq, last_seq)
                self._session.commit()
                return row.segment_id, row.followed_since
        earlier = self._session.scalar(
            select(StreamFollowRow)
            .where(StreamFollowRow.epoch == epoch)
            .order_by(desc(StreamFollowRow.last_seq), desc(StreamFollowRow.segment_id))
            .limit(1)
        )
        if earlier is not None and first_seq <= earlier.last_seq + 1:
            since = min(since, earlier.followed_since)
        row = StreamFollowRow(
            epoch=epoch, first_seq=first_seq, last_seq=last_seq, followed_since=since
        )
        self._session.add(row)
        self._session.commit()
        return row.segment_id, row.followed_since


class EventRepository:
    """Append-only journal of observed Kubernetes events.

    A coalesced repeat (Kubernetes bumps ``count``/``lastTimestamp`` on the
    same event object instead of creating a new one) is stored again under a
    new dedup key, mirroring how the object journal treats each observed
    version; an exact repeat is skipped.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        body: dict[str, Any],
        observed_at: datetime,
        *,
        connector_observed_at: datetime | None = None,
    ) -> bool:
        involved = child(body, "involvedObject")
        kind, name = involved.get("kind"), involved.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            return False
        namespace = str(involved.get("namespace") or CLUSTER_SCOPE)
        identity = event_identity(body, namespace)
        key = event_version_key(identity, body)
        exists = self._session.scalar(
            select(EventVersionRow.version_id).where(
                EventVersionRow.namespace == namespace, EventVersionRow.dedup_key == key
            )
        )
        if exists is not None:
            return False
        event_at = (
            _timestamp(body.get("firstTimestamp"))
            or _timestamp(body.get("lastTimestamp"))
            or _timestamp(body.get("eventTime"))
            or observed_at
        )
        row = EventVersionRow(
            namespace=namespace,
            involved_kind=kind,
            involved_name=name,
            involved_uid=_nested_uid(body, "involvedObject"),
            dedup_key=key,
            event_at=event_at,
            observed_at=observed_at,
            body=body,
        )
        self._session.add(row)
        _record_arrival(self._session, "event", row, connector_observed_at)
        self._session.commit()
        return True

    def history_versions(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Return every persisted Event version observed by the cutoff.

        ``event_at`` describes when Kubernetes says the Event occurred.  It is
        not the replay boundary: an Event can be updated after an incident has
        resolved while retaining an old ``firstTimestamp``.  The lower bound
        keeps the incident lookback useful for Events whose occurrence began
        before the window but whose state was first observed during it.
        """
        rows = self._window_rows(namespaces=namespaces, starts_at=starts_at, ends_at=ends_at)
        return [
            _body_with_persisted_uid(row.body, "involvedObject", row.involved_uid) for row in rows
        ]

    def _window_rows(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[EventVersionRow]:
        """Event versions observed by the cutoff, by their effective observation time (C9 §3)."""
        (arrival, on), effective = _arrival("event", EventVersionRow)
        return list(
            self._session.scalars(
                select(EventVersionRow)
                .outerjoin(arrival, on)
                .where(
                    EventVersionRow.namespace.in_(namespaces),
                    effective <= ends_at,
                    (EventVersionRow.event_at >= starts_at) | (effective >= starts_at),
                )
                .order_by(effective, EventVersionRow.version_id)
            ).all()
        )

    def analysis_view(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Return the latest visible state of each logical Kubernetes Event.

        The append-only journal is deliberately kept separate from this view:
        coalesced Kubernetes updates remain available for provenance, while
        RCA receives one state per stable Event identity at the frozen cutoff.
        """
        return [
            _event_body(row)
            for row in self._analysis_rows(
                namespaces=namespaces, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def analysis_version_ids(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[int]:
        """Exact ids of the Event versions ``analysis_view`` returns for this window."""
        return [
            row.version_id
            for row in self._analysis_rows(
                namespaces=namespaces, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def bodies(self, version_ids: Sequence[int]) -> list[tuple[int, dict[str, Any]]]:
        """Exactly these Event versions as ``(version_id, body)``, in version order."""
        if not version_ids:
            return []
        rows = self._session.scalars(
            select(EventVersionRow)
            .where(EventVersionRow.version_id.in_(version_ids))
            .order_by(EventVersionRow.version_id)
        ).all()
        return [(row.version_id, _event_body(row)) for row in rows]

    def _analysis_rows(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[EventVersionRow]:
        rows = self._window_rows(namespaces=namespaces, starts_at=starts_at, ends_at=ends_at)
        latest: dict[str, EventVersionRow] = {}
        for row in rows:
            identity = event_identity(row.body, row.namespace)
            latest[identity] = row
        return sorted(latest.values(), key=lambda item: item.version_id)

    def history(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Compatibility name for the deduplicated Event analysis/replay view."""
        return self.analysis_view(namespaces=namespaces, starts_at=starts_at, ends_at=ends_at)


@dataclass(frozen=True)
class PersistedSnapshotCycle:
    """One snapshot cycle as persisted: the only form RCA receives it in."""

    cycle_id: int
    observed_at: datetime
    completed_at: datetime
    objects: tuple[dict[str, Any], ...]


class SnapshotCycleRepository:
    """Diagnosis capture cycles and the exact bodies they listed (authoritative evidence).

    A cycle is written once, complete: its row and every listed object in a
    single transaction, never inserted early and updated later.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        run_id: str,
        started_at: datetime,
        observed_at: datetime,
        completed_at: datetime,
        completed_scopes: Sequence[tuple[str, str]],
        failed_scopes: Sequence[tuple[str, str, str]],
        objects: Sequence[dict[str, Any]],
    ) -> int:
        """Persist one completed cycle with full bodies; returns its ``cycle_id``."""
        cycle = SnapshotCycleRow(
            run_id=run_id,
            started_at=started_at,
            observed_at=observed_at,
            completed_at=completed_at,
            completed_scopes=[list(scope) for scope in sorted(completed_scopes)],
            failed_scopes=[
                {"namespace": namespace, "kind": kind, "error": error}
                for namespace, kind, error in failed_scopes
            ],
        )
        self._session.add(cycle)
        self._session.flush()
        seen: set[str] = set()
        for body in objects:
            key = object_key(body)
            if key is None or key in seen:
                continue  # one body per object and cycle
            seen.add(key)
            namespace, kind, name = key.split("/", 2)
            self._session.add(
                SnapshotCycleObjectRow(
                    cycle_id=cycle.cycle_id,
                    object_key=key,
                    namespace=namespace,
                    kind=kind,
                    name=name,
                    uid=_nested_uid(body, "metadata"),
                    body=body,
                    evidence_id=snapshot_evidence_id(cycle.cycle_id, key),
                )
            )
        self._session.commit()
        return cycle.cycle_id

    def load(self, cycle_id: int) -> PersistedSnapshotCycle:
        """A persisted cycle and its objects exactly as stored, in key order."""
        cycle = self._session.get(SnapshotCycleRow, cycle_id)
        if cycle is None:
            raise LookupError(f"snapshot cycle {cycle_id} was not found")
        rows = self._session.scalars(
            select(SnapshotCycleObjectRow)
            .where(SnapshotCycleObjectRow.cycle_id == cycle_id)
            .order_by(SnapshotCycleObjectRow.object_key)
        ).all()
        return PersistedSnapshotCycle(
            cycle_id=cycle.cycle_id,
            observed_at=cycle.observed_at,
            completed_at=cycle.completed_at,
            objects=tuple(dict(row.body) for row in rows),
        )


class ObjectVersionRepository:
    """Append-only journal of object versions and lifecycle events.

    Writers must be serialized (the control plane holds a lock around snapshots).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def content_hash(body: dict[str, Any]) -> str:
        return object_content_hash(body)

    def _latest(self, key: str) -> ObjectVersionRow | None:
        return self._session.scalars(
            select(ObjectVersionRow)
            .where(ObjectVersionRow.object_key == key)
            .order_by(desc(ObjectVersionRow.version_id))
            .limit(1)
        ).first()

    def _recording_started(self, namespace: str) -> datetime | None:
        return self._session.scalar(
            select(func.min(ObjectVersionRow.observed_at)).where(
                ObjectVersionRow.namespace == namespace
            )
        )

    def record(
        self,
        body: dict[str, Any],
        observed_at: datetime,
        *,
        connector_observed_at: datetime | None = None,
    ) -> bool:
        """Store ``body`` unless it equals the object's latest live version."""
        metadata = child(body, "metadata")
        kind, name = body.get("kind"), metadata.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            raise ValueError("object body needs kind and metadata.name")
        namespace = str(metadata.get("namespace") or CLUSTER_SCOPE)
        key = f"{namespace}/{kind}/{name}"
        digest = self.content_hash(body)
        uid = _nested_uid(body, "metadata")
        latest = self._latest(key)
        if latest is None:
            started = self._recording_started(namespace)
            created = _timestamp(metadata.get("creationTimestamp"))
            lifecycle = (
                Lifecycle.CREATED
                if started is not None and created is not None and created > started
                else Lifecycle.OBSERVED
            )
        elif latest.lifecycle == Lifecycle.DELETED:
            lifecycle = Lifecycle.CREATED
        elif latest.content_hash == digest and latest.uid == uid:
            # Same desired state of the same instance. The same name and spec with
            # another (or an appearing/disappearing) UID is a new observation.
            return False
        else:
            lifecycle = Lifecycle.UPDATED
        row = ObjectVersionRow(
            object_key=key,
            namespace=namespace,
            kind=kind,
            name=name,
            uid=uid,
            observed_at=observed_at,
            content_hash=digest,
            body=body,
            lifecycle=lifecycle.value,
        )
        self._session.add(row)
        _record_arrival(self._session, "object", row, connector_observed_at)
        self._session.commit()
        return True

    def tombstones_missing_deletion(self, namespaces: set[str]) -> list[TombstoneRef]:
        """Pod journal tombstones whose exact UID has no DELETED lifecycle row yet."""
        recorded = (
            select(LifecycleObservationRow.observation_id)
            .where(
                LifecycleObservationRow.type == "DELETED",
                LifecycleObservationRow.namespace == ObjectVersionRow.namespace,
                LifecycleObservationRow.kind == ObjectVersionRow.kind,
                LifecycleObservationRow.instance_uid == ObjectVersionRow.uid,
            )
            .exists()
        )
        return _tombstones(self._session, namespaces, "Pod", recorded)

    def tombstone(self, key: str, observed_at: datetime) -> bool:
        """Record that a live object is gone; its last body is kept as the tombstone body."""
        latest = self._latest(key)
        if latest is None or latest.lifecycle == Lifecycle.DELETED:
            return False
        self._session.add(
            ObjectVersionRow(
                object_key=key,
                namespace=latest.namespace,
                kind=latest.kind,
                name=latest.name,
                uid=latest.uid,
                observed_at=observed_at,
                content_hash=latest.content_hash,
                body=latest.body,
                lifecycle=Lifecycle.DELETED.value,
            )
        )
        self._session.commit()
        return True

    def live_keys(self, namespaces: set[str]) -> set[str]:
        """Object keys in these namespaces whose latest journal version is not a tombstone."""
        newest = (
            select(func.max(ObjectVersionRow.version_id))
            .where(ObjectVersionRow.namespace.in_(namespaces))
            .group_by(ObjectVersionRow.object_key)
        )
        rows = self._session.execute(
            select(ObjectVersionRow.object_key, ObjectVersionRow.lifecycle).where(
                ObjectVersionRow.version_id.in_(newest)
            )
        ).all()
        return {key for key, lifecycle in rows if lifecycle != Lifecycle.DELETED}

    def history(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[JournalEntry]:
        """Versions in the window plus each object's last version before it, oldest first."""
        return [
            _journal_entry(row, effective)
            for row, effective in self._history_rows(
                namespaces=namespaces, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def history_version_ids(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[int]:
        """Exact ids of the versions ``history`` returns for this window."""
        return [
            row.version_id
            for row, _effective in self._history_rows(
                namespaces=namespaces, starts_at=starts_at, ends_at=ends_at
            )
        ]

    def entries(self, version_ids: Sequence[int]) -> list[JournalEntry]:
        """Exactly these journal versions, oldest first."""
        if not version_ids:
            return []
        (arrival, on), effective = _arrival("object", ObjectVersionRow)
        rows = self._session.execute(
            select(ObjectVersionRow, effective)
            .outerjoin(arrival, on)
            .where(ObjectVersionRow.version_id.in_(version_ids))
            .order_by(effective, ObjectVersionRow.version_id)
        ).all()
        typed = cast(list[tuple[ObjectVersionRow, datetime]], [tuple(r) for r in rows])
        return [_journal_entry(row, at) for row, at in typed]

    def _history_rows(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[tuple[ObjectVersionRow, datetime]]:
        """Each row with its effective observation time (late-evidence-design.md §3)."""
        (arrival, on), effective = _arrival("object", ObjectVersionRow)
        rows = self._session.execute(
            select(ObjectVersionRow, effective)
            .outerjoin(arrival, on)
            .where(
                ObjectVersionRow.namespace.in_(namespaces | {CLUSTER_SCOPE}),
                effective <= ends_at,
            )
            .order_by(effective, ObjectVersionRow.version_id)
        ).all()
        typed = cast(list[tuple[ObjectVersionRow, datetime]], [tuple(r) for r in rows])
        baseline: dict[str, tuple[ObjectVersionRow, datetime]] = {}
        selected: list[tuple[ObjectVersionRow, datetime]] = []
        for row, at in typed:
            if at < starts_at:
                baseline[row.object_key] = (row, at)
            else:
                selected.append((row, at))
        # Objects already deleted before the window are not part of the incident.
        before = [item for item in baseline.values() if item[0].lifecycle != Lifecycle.DELETED]
        return sorted([*before, *selected], key=lambda item: (item[1], item[0].version_id))


def _journal_entry(row: ObjectVersionRow, observed_at: datetime | None = None) -> JournalEntry:
    return JournalEntry(
        object_key=row.object_key,
        # the effective observation time (C9 §3) when the caller has it
        observed_at=observed_at if observed_at is not None else row.observed_at,
        # JournalEntry is the existing storage-to-source contract; project
        # the first-class UID column into its body for the live model mapper.
        # The stored JSON body is not used as the UID source of truth.
        body=_body_with_persisted_uid(row.body, "metadata", row.uid),
        version_id=row.version_id,
        lifecycle=Lifecycle(row.lifecycle),
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class LifecycleRecord:
    """One persisted lifecycle observation of one exact instance."""

    evidence_id: str
    instance_uid: str
    namespace: str
    kind: str
    name: str
    type: str
    source_at: datetime | None
    observed_at: datetime
    ingested_at: datetime
    source: str
    payload: dict[str, Any]


def _lifecycle_record(row: LifecycleObservationRow) -> LifecycleRecord:
    return LifecycleRecord(
        evidence_id=row.evidence_id,
        instance_uid=row.instance_uid,
        namespace=row.namespace,
        kind=row.kind,
        name=row.name,
        type=row.type,
        source_at=row.source_at,
        observed_at=row.observed_at,
        ingested_at=row.ingested_at,
        source=row.source,
        payload=dict(row.payload),
    )


@dataclass(frozen=True)
class TombstoneRef:
    """A persisted journal tombstone of one exact instance, as the journal recorded it."""

    namespace: str
    kind: str
    name: str
    uid: str
    observed_at: datetime


def _tombstones(
    session: Session, namespaces: set[str], kind: str | None, exclude: Any
) -> list[TombstoneRef]:
    """Earliest real journal tombstone per exact instance that ``exclude`` does not cover."""
    query = select(ObjectVersionRow).where(
        ObjectVersionRow.lifecycle == Lifecycle.DELETED.value,
        ObjectVersionRow.uid.is_not(None),
        ObjectVersionRow.namespace.in_(namespaces),
        ~exclude,
    )
    if kind is not None:
        query = query.where(ObjectVersionRow.kind == kind)
    found: dict[tuple[str, str, str], TombstoneRef] = {}
    for row in session.scalars(
        query.order_by(ObjectVersionRow.observed_at, ObjectVersionRow.version_id)
    ):
        assert row.uid is not None
        found.setdefault(
            (row.namespace, row.kind, row.uid),
            TombstoneRef(row.namespace, row.kind, row.name, row.uid, row.observed_at),
        )
    return list(found.values())


# Concurrent writers of one instance may race for the same sequence number.
_LIFECYCLE_APPEND_ATTEMPTS = 5


class LifecycleRepository:
    """Append-only ledger of lifecycle observations (authoritative evidence).

    Rows are only ever inserted: there is deliberately no update or delete.
    Concurrent writers may collide on an instance's next sequence number; the
    loser re-reads it and retries, so a different fact is never dropped.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _next_sequence(self, namespace: str, kind: str, uid: str) -> int:
        prefix = f"lifecycle:{namespace}:{kind}:{uid}:"
        ids = self._session.scalars(
            select(LifecycleObservationRow.evidence_id).where(
                LifecycleObservationRow.evidence_id.startswith(prefix, autoescape=True)
            )
        ).all()
        used = [int(item[len(prefix) :]) for item in ids if item[len(prefix) :].isdigit()]
        return max(used, default=0) + 1

    def _evidence_id_taken(self, evidence_id: str) -> bool:
        return (
            self._session.scalar(
                select(LifecycleObservationRow.observation_id).where(
                    LifecycleObservationRow.evidence_id == evidence_id
                )
            )
            is not None
        )

    def _exists(self, uid: str, type_: str, observed_at: datetime, source: str) -> bool:
        return (
            self._session.scalar(
                select(LifecycleObservationRow.observation_id).where(
                    LifecycleObservationRow.instance_uid == uid,
                    LifecycleObservationRow.type == type_,
                    LifecycleObservationRow.observed_at == observed_at,
                    LifecycleObservationRow.source == source,
                )
            )
            is not None
        )

    def append(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        instance_uid: str,
        type: str,
        observed_at: datetime,
        source: str,
        payload: dict[str, Any],
        source_at: datetime | None = None,
        ingested_at: datetime | None = None,
    ) -> LifecycleRecord | None:
        """Insert one observation; ``None`` when the same fact is already recorded."""
        if type not in LIFECYCLE_OBSERVATION_TYPES:
            raise ValueError(f"unknown lifecycle observation type: {type}")
        if not instance_uid:
            raise ValueError("lifecycle observations need the exact instance uid")
        for _ in range(_LIFECYCLE_APPEND_ATTEMPTS):
            if self._exists(instance_uid, type, observed_at, source):
                return None
            sequence = self._next_sequence(namespace, kind, instance_uid)
            evidence_id = f"lifecycle:{namespace}:{kind}:{instance_uid}:{sequence}"
            row = LifecycleObservationRow(
                evidence_id=evidence_id,
                instance_uid=instance_uid,
                namespace=namespace,
                kind=kind,
                name=name,
                type=type,
                source_at=source_at,
                observed_at=observed_at,
                ingested_at=ingested_at or datetime.now(UTC),
                source=source,
                payload=payload,
            )
            self._session.add(row)
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                if self._exists(instance_uid, type, observed_at, source):
                    return None
                # Another writer took this sequence for a different fact:
                # re-read the sequence and try again rather than lose evidence.
                if self._evidence_id_taken(evidence_id):
                    continue
                raise
            return _lifecycle_record(row)
        raise RuntimeError(
            f"could not allocate a lifecycle sequence for {namespace}/{kind}/{instance_uid} "
            f"after {_LIFECYCLE_APPEND_ATTEMPTS} attempts"
        )

    def list_for(self, namespace: str, kind: str, uid: str) -> list[LifecycleRecord]:
        """Every observation of one exact instance, oldest first."""
        rows = self._session.scalars(
            select(LifecycleObservationRow)
            .where(
                LifecycleObservationRow.namespace == namespace,
                LifecycleObservationRow.kind == kind,
                LifecycleObservationRow.instance_uid == uid,
            )
            .order_by(LifecycleObservationRow.observed_at, LifecycleObservationRow.observation_id)
        ).all()
        return [_lifecycle_record(row) for row in rows]

    def list_window(
        self, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[LifecycleRecord]:
        """Observations in these namespaces observed within ``[starts_at, ends_at]``."""
        rows = self._session.scalars(
            select(LifecycleObservationRow)
            .where(
                LifecycleObservationRow.namespace.in_(namespaces),
                LifecycleObservationRow.observed_at >= starts_at,
                LifecycleObservationRow.observed_at <= ends_at,
            )
            .order_by(LifecycleObservationRow.observed_at, LifecycleObservationRow.observation_id)
        ).all()
        return [_lifecycle_record(row) for row in rows]


class EntityInstanceRepository:
    """Materialized index of exact instances; updated in place, never evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def tombstones_unmarked(self, namespaces: set[str]) -> list[TombstoneRef]:
        """Journal tombstones whose exact instance is not yet marked deleted in the index."""
        marked = (
            select(EntityInstanceRow.instance_id)
            .where(
                EntityInstanceRow.namespace == ObjectVersionRow.namespace,
                EntityInstanceRow.kind == ObjectVersionRow.kind,
                EntityInstanceRow.uid == ObjectVersionRow.uid,
                EntityInstanceRow.deleted_observed_at.is_not(None),
            )
            .exists()
        )
        return _tombstones(self._session, namespaces, None, marked)

    def upsert(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        uid: str,
        observed_at: datetime,
        owner_kind: str | None = None,
        owner_name: str | None = None,
        owner_uid: str | None = None,
        created_at: datetime | None = None,
        deleted: bool = False,
    ) -> None:
        """Record a sighting (or the deletion) of one exact instance."""
        if not uid:
            raise ValueError("entity instances need the exact uid")
        row = self._session.scalars(
            select(EntityInstanceRow).where(
                EntityInstanceRow.namespace == namespace,
                EntityInstanceRow.kind == kind,
                EntityInstanceRow.uid == uid,
            )
        ).first()
        if row is None:
            row = EntityInstanceRow(
                namespace=namespace,
                kind=kind,
                name=name,
                uid=uid,
                first_observed_at=observed_at,
                last_observed_at=observed_at,
            )
            self._session.add(row)
        else:
            row.first_observed_at = min(row.first_observed_at, observed_at)
            row.last_observed_at = max(row.last_observed_at, observed_at)
        row.owner_kind = owner_kind if owner_kind is not None else row.owner_kind
        row.owner_name = owner_name if owner_name is not None else row.owner_name
        row.owner_uid = owner_uid if owner_uid is not None else row.owner_uid
        row.created_at = created_at if created_at is not None else row.created_at
        if deleted and row.deleted_observed_at is None:
            row.deleted_observed_at = observed_at
        self._session.commit()


# One insert plus at most three retries after a lost revision-number race.
_REVISION_ATTEMPTS = 4


@dataclass(frozen=True)
class DiagnosisRevision:
    """The identity of one written diagnosis revision."""

    diagnosis_id: int
    revision_number: int


class DiagnosisRepository:
    """Stored diagnoses per incident."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_revision(
        self,
        *,
        incident_id: object,
        document: dict[str, Any],
        created_at: datetime,
        run_id: str | None,
        trigger: str,
        window_end: datetime,
        manifest_digest: str,
        tape_digest: str,
        epistemic_digest: str,
        engine_version: str,
        config_digest: str,
        companions: Callable[[Session, int], None] | None = None,
    ) -> DiagnosisRevision:
        """Append the incident's next immutable revision and commit it.

        One transaction per attempt: lock the incident row, read the highest
        revision, insert and flush the next one, write any ``companions`` rows
        with the new ``diagnosis_id``, commit. A concurrent writer that wins the
        same number makes the insert fail on ``UNIQUE(incident_id,
        revision_number)``; that is retried at most three times. A companion
        failure rolls the whole revision back and is not retried. Diagnoses are
        never updated.
        """
        if trigger not in DIAGNOSIS_TRIGGERS or trigger == "LEGACY":
            raise ValueError(f"not a revision trigger: {trigger!r}")
        for attempt in range(_REVISION_ATTEMPTS):
            incident = self._session.scalars(
                select(IncidentRow.incident_id)
                .where(IncidentRow.incident_id == incident_id)
                .with_for_update()
            ).first()
            if incident is None:
                self._session.rollback()
                raise IncidentNotFoundError(str(incident_id))
            previous = self._session.execute(
                select(DiagnosisRow.diagnosis_id, DiagnosisRow.revision_number)
                .where(DiagnosisRow.incident_id == incident_id)
                .order_by(desc(DiagnosisRow.revision_number))
                .limit(1)
            ).first()
            row = DiagnosisRow(
                incident_id=incident_id,
                created_at=created_at,
                root_cause=document.get("root_cause") and _canonical(document["root_cause"]),
                confidence=str(document.get("confidence")),
                mode=str(document.get("mode")),
                run_id=run_id,
                document=document,
                revision_number=previous.revision_number + 1 if previous else 1,
                previous_diagnosis_id=previous.diagnosis_id if previous else None,
                trigger=trigger,
                window_end=window_end,
                manifest_digest=manifest_digest,
                tape_digest=tape_digest,
                epistemic_digest=epistemic_digest,
                engine_version=engine_version,
                config_digest=config_digest,
            )
            self._session.add(row)
            try:
                self._session.flush()
            except IntegrityError:
                self._session.rollback()
                if attempt + 1 == _REVISION_ATTEMPTS:
                    raise
                continue
            revision = DiagnosisRevision(row.diagnosis_id, row.revision_number)
            try:
                if companions is not None:
                    companions(self._session, row.diagnosis_id)
                self._session.commit()
            except BaseException:
                self._session.rollback()
                raise
            return revision
        raise AssertionError("unreachable")

    def revision_count(self, incident_id: object) -> int:
        """How many revisions the incident has, legacy ones included."""
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(DiagnosisRow)
                .where(DiagnosisRow.incident_id == incident_id)
            )
            or 0
        )

    def deadline_consumed(
        self, *, incident_id: object, after_diagnosis_id: int, not_before: datetime
    ) -> bool:
        """Whether a later EVIDENCE_DEADLINE revision already evaluated this deadline.

        Only a scheduler revision newer than the requirement's opening revision
        whose window reaches ``not_before`` consumes it; MANUAL and INITIAL
        revisions never do.
        """
        return (
            self._session.scalars(
                select(DiagnosisRow.diagnosis_id)
                .where(
                    DiagnosisRow.incident_id == incident_id,
                    DiagnosisRow.trigger == "EVIDENCE_DEADLINE",
                    DiagnosisRow.diagnosis_id > after_diagnosis_id,
                    DiagnosisRow.window_end >= not_before,
                )
                .limit(1)
            ).first()
            is not None
        )

    def latest(self, incident_id: object) -> dict[str, Any] | None:
        row = self._session.scalars(
            select(DiagnosisRow)
            .where(DiagnosisRow.incident_id == incident_id)
            .order_by(desc(DiagnosisRow.created_at), desc(DiagnosisRow.diagnosis_id))
            .limit(1)
        ).first()
        return dict(row.document) if row is not None else None

    def list_revisions(self, incident_id: object) -> list[DiagnosisRow]:
        """Return persisted revisions for one incident in authoritative number order."""
        return list(
            self._session.scalars(
                select(DiagnosisRow)
                .where(DiagnosisRow.incident_id == incident_id)
                .order_by(DiagnosisRow.revision_number.asc())
            ).all()
        )

    def get_revision(self, incident_id: object, revision_number: int) -> DiagnosisRow | None:
        """Load a revision by its incident-scoped revision number."""
        return self._session.scalars(
            select(DiagnosisRow).where(
                DiagnosisRow.incident_id == incident_id,
                DiagnosisRow.revision_number == revision_number,
            )
        ).first()

    def get_revision_by_id(self, incident_id: object, diagnosis_id: int) -> DiagnosisRow | None:
        """Load a previous-link target only when it belongs to the same incident."""
        return self._session.scalars(
            select(DiagnosisRow).where(
                DiagnosisRow.incident_id == incident_id,
                DiagnosisRow.diagnosis_id == diagnosis_id,
            )
        ).first()

    def latest_created_at(self, incident_id: object) -> datetime | None:
        """When the latest diagnosis was stored, for lifecycle timing."""
        return self._session.scalars(
            select(DiagnosisRow.created_at)
            .where(DiagnosisRow.incident_id == incident_id)
            .order_by(desc(DiagnosisRow.created_at), desc(DiagnosisRow.diagnosis_id))
            .limit(1)
        ).first()

    def latest_run_id(self, incident_id: object) -> str | None:
        """The pipeline run id of the latest stored diagnosis, to bind its timeline."""
        return self._session.scalars(
            select(DiagnosisRow.run_id)
            .where(DiagnosisRow.incident_id == incident_id)
            .order_by(desc(DiagnosisRow.created_at), desc(DiagnosisRow.diagnosis_id))
            .limit(1)
        ).first()

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

    def latest_views(self) -> dict[str, dict[str, Any]]:
        """Latest diagnosis view per incident for list/dashboard rendering.

        One query for every incident's diagnoses, reduced to the newest per
        incident in Python. ``resolution`` and the affected ``services`` live in
        the stored document, so they are read from it rather than joined; this
        keeps the list at two queries (incidents + this) with no per-row fetch.
        """
        result: dict[str, dict[str, Any]] = {}
        for row in self._session.scalars(
            select(DiagnosisRow).order_by(DiagnosisRow.created_at, DiagnosisRow.diagnosis_id)
        ).all():
            document = row.document
            symptoms = document.get("symptoms") or {}
            services = symptoms.get("services") or ()
            result[str(row.incident_id)] = {
                "root_cause": row.root_cause,
                "confidence": row.confidence,
                "resolution": document.get("resolution"),
                # roadmap C10: absent from documents written before the field existed
                "leading_actor_withheld_reason": document.get("leading_actor_withheld_reason"),
                "leading_actor_display": document.get("leading_actor_display"),
                "leading_actor_candidates": tuple(
                    _canonical(ref) for ref in document.get("leading_actor_candidates") or ()
                ),
                "services": tuple(services),
                "created_at": row.created_at,
                "run_id": row.run_id,
            }
        return result


@dataclass(frozen=True)
class RequirementTransitions:
    """What one revision did to its incident's requirements, by requirement_key."""

    opened: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    satisfied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()


class EvidenceRequirementRepository:
    """Requirement lifecycle driven by one diagnosis revision (M19-5.5).

    Runs inside the revision's transaction, after the diagnosis row is
    flushed and while the incident row is locked. Matching is by exact
    ``requirement_key`` only; the previous OPEN row of a key:

    * PASS or DISQUALIFIED → SATISFIED_BY_REVISION
    * PENDING → SUPERSEDED_BY_REVISION and a new OPEN row for the same key
    * deadline NO_DATA/PARTIAL → unchanged
    * SCOPE_EXITED, or its hypothesis_key absent from the full inventory →
      SUPERSEDED_BY_REVISION, nothing reopened
    * hypothesis_key repeated in the inventory, or no current evaluation →
      unchanged

    A PENDING evaluation without a unique hypothesis_key creates no row.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def apply_revision(
        self, *, incident_id: UUID, diagnosis_id: int, diagnosis: Diagnosis
    ) -> RequirementTransitions:
        counts = Counter(
            entry.hypothesis_key
            for entry in diagnosis.hypothesis_inventory
            if entry.hypothesis_key is not None
        )
        current: dict[str, RequirementEvaluation] = {}
        for evaluation in diagnosis.requirement_evaluations:
            if evaluation.hypothesis_key is None or counts[evaluation.hypothesis_key] != 1:
                continue  # no unique identity: never fabricated, never matched
            key = requirement_key(incident_id, evaluation)
            if current.setdefault(key, evaluation) != evaluation:
                raise ValueError(f"conflicting evaluations for requirement {key}")
        opened: list[str] = []
        superseded: list[str] = []
        satisfied: list[str] = []
        unchanged: list[str] = []
        rows = self._session.scalars(
            select(EvidenceRequirementRow)
            .where(
                EvidenceRequirementRow.incident_id == incident_id,
                EvidenceRequirementRow.status == "OPEN",
            )
            .order_by(EvidenceRequirementRow.requirement_id)
            .with_for_update()
        ).all()
        for row in rows:
            count = counts.get(row.hypothesis_key, 0)
            matched = current.get(row.requirement_key) if count == 1 else None
            if count == 0 or (
                matched is not None and matched.audit_reason is RequirementAuditReason.SCOPE_EXITED
            ):
                row.status = "SUPERSEDED_BY_REVISION"
                superseded.append(row.requirement_key)
            elif matched is None or matched.result is None:
                unchanged.append(row.requirement_key)
            elif matched.result.status is PreconditionStatus.PENDING:
                row.status = "SUPERSEDED_BY_REVISION"
                superseded.append(row.requirement_key)
            else:
                row.status = "SATISFIED_BY_REVISION"
                satisfied.append(row.requirement_key)
        # Close before reopening: at most one OPEN row per key.
        self._session.flush()
        still_open = set(unchanged)
        for key, evaluation in current.items():
            result = evaluation.result
            if result is None or result.status is not PreconditionStatus.PENDING:
                continue
            if key in still_open:
                continue
            assert result.not_before is not None
            assert evaluation.hypothesis_key is not None
            self._session.add(
                EvidenceRequirementRow(
                    requirement_key=key,
                    incident_id=incident_id,
                    diagnosis_id=diagnosis_id,
                    hypothesis_key=evaluation.hypothesis_key,
                    rule_id=evaluation.rule_id,
                    rule_version=evaluation.rule_version,
                    kind=evaluation.kind.value,
                    targets=requirement_targets_document(evaluation),
                    not_before=result.not_before,
                    status="OPEN",
                )
            )
            opened.append(key)
        self._session.flush()
        return RequirementTransitions(
            opened=tuple(opened),
            superseded=tuple(superseded),
            satisfied=tuple(satisfied),
            unchanged=tuple(unchanged),
        )

    def open_requirements(self) -> list[EvidenceRequirementRow]:
        """Every OPEN requirement, in incident then requirement order."""
        return list(
            self._session.scalars(
                select(EvidenceRequirementRow)
                .where(EvidenceRequirementRow.status == "OPEN")
                .order_by(EvidenceRequirementRow.incident_id, EvidenceRequirementRow.requirement_id)
            )
        )

    def opening_onset(self, requirement: EvidenceRequirementRow) -> datetime:
        """``symptoms.onset`` persisted in the revision that opened the requirement.

        The horizon origin of the requirement. A missing or unreadable onset is
        persisted-state corruption and raises; nothing is substituted.
        """
        opening = self._session.get(DiagnosisRow, requirement.diagnosis_id)
        if opening is None or opening.incident_id != requirement.incident_id:
            raise RequirementOnsetUnavailable(
                f"requirement {requirement.requirement_id}: opening revision "
                f"{requirement.diagnosis_id} is missing for its incident"
            )
        try:
            onset = Symptoms.model_validate(opening.document["symptoms"]).onset
        except (KeyError, TypeError, ValueError) as error:
            raise RequirementOnsetUnavailable(
                f"requirement {requirement.requirement_id}: opening revision "
                f"{requirement.diagnosis_id} has no readable symptoms"
            ) from error
        if onset is None or onset.tzinfo is None:
            raise RequirementOnsetUnavailable(
                f"requirement {requirement.requirement_id}: opening revision "
                f"{requirement.diagnosis_id} has no onset"
            )
        return onset

    def expire(self, requirement_ids: Sequence[int]) -> None:
        """Mark OPEN requirements EXPIRED; the scheduler's only requirement write."""
        for row in self._session.scalars(
            select(EvidenceRequirementRow).where(
                EvidenceRequirementRow.requirement_id.in_(list(requirement_ids)),
                EvidenceRequirementRow.status == "OPEN",
            )
        ):
            row.status = "EXPIRED"
        self._session.flush()

    def for_incident(self, incident_id: UUID) -> list[EvidenceRequirementRow]:
        return list(
            self._session.scalars(
                select(EvidenceRequirementRow)
                .where(EvidenceRequirementRow.incident_id == incident_id)
                .order_by(EvidenceRequirementRow.requirement_id)
            )
        )


class ReportRepository:
    """Immutable incident report snapshots.

    A snapshot is written once and never updated; a new report of the same
    incident is a new row with its own ``report_id``. This preserves the history
    of what was reported for each diagnosis run.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        *,
        report_id: str,
        incident_id: object,
        diagnosis_run_id: str | None,
        report_version: str,
        created_at: datetime,
        document: dict[str, Any],
    ) -> None:
        self._session.add(
            ReportRow(
                report_id=report_id,
                incident_id=incident_id,
                diagnosis_run_id=diagnosis_run_id,
                report_version=report_version,
                created_at=created_at,
                document=document,
            )
        )
        self._session.commit()

    def get(self, report_id: str) -> dict[str, Any] | None:
        row = self._session.get(ReportRow, report_id)
        return dict(row.document) if row is not None else None

    def list_all(
        self, *, incident_id: object | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Newest reports first, optionally scoped to one incident."""
        if limit < 1:
            raise ValueError("limit must be positive")
        statement = select(ReportRow)
        if incident_id is not None:
            statement = statement.where(ReportRow.incident_id == incident_id)
        rows = self._session.scalars(
            statement.order_by(desc(ReportRow.created_at), desc(ReportRow.report_id)).limit(limit)
        ).all()
        return [dict(row.document) for row in rows]


class InvestigationRunRepository:
    """Append-only persistence for a structured bounded-investigation result."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        *,
        diagnosis_run_id: str,
        incident_id: object,
        artifact_version: str,
        created_at: datetime,
        document: dict[str, Any],
        commit: bool = True,
    ) -> None:
        if not diagnosis_run_id:
            raise ValueError("diagnosis_run_id is required")
        if not artifact_version:
            raise ValueError("artifact_version is required")
        self._session.add(
            InvestigationRunRow(
                diagnosis_run_id=diagnosis_run_id,
                incident_id=incident_id,
                artifact_version=artifact_version,
                created_at=created_at,
                document=document,
            )
        )
        if commit:
            self._session.commit()

    def get(self, diagnosis_run_id: str) -> dict[str, Any] | None:
        row = self._session.get(InvestigationRunRow, diagnosis_run_id)
        if row is None:
            return None
        return {
            "diagnosis_run_id": row.diagnosis_run_id,
            "incident_id": str(row.incident_id),
            "artifact_version": row.artifact_version,
            "created_at": row.created_at.isoformat(),
            "document": dict(row.document),
        }

    def latest_for_incident(self, incident_id: object) -> dict[str, Any] | None:
        row = self._session.scalars(
            select(InvestigationRunRow)
            .where(InvestigationRunRow.incident_id == incident_id)
            .order_by(
                desc(InvestigationRunRow.created_at), desc(InvestigationRunRow.diagnosis_run_id)
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        return {
            "diagnosis_run_id": row.diagnosis_run_id,
            "incident_id": str(row.incident_id),
            "artifact_version": row.artifact_version,
            "created_at": row.created_at.isoformat(),
            "document": dict(row.document),
        }


class EmailDeliveryRepository:
    """Audit log of report shares over email."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_report_and_key(self, report_id: str, key: str) -> dict[str, Any] | None:
        """Find a prior delivery for this report and idempotency key (a replay)."""
        row = self._session.scalars(
            select(EmailDeliveryRow)
            .where(
                EmailDeliveryRow.report_id == report_id,
                EmailDeliveryRow.idempotency_key == key,
            )
            .limit(1)
        ).first()
        return _delivery_to_dict(row) if row is not None else None

    def reserve(
        self,
        *,
        delivery_id: str,
        report_id: str,
        incident_id: object | None,
        recipients: list[str],
        subject: str,
        idempotency_key: str | None,
        created_at: datetime,
    ) -> dict[str, Any] | None:
        """Atomically claim a delivery in ``pending`` before any send is attempted.

        The ``idempotency_key`` unique constraint makes this the concurrency
        gate: of two racing requests with the same key, exactly one insert
        succeeds and owns the send; the loser gets ``None`` and returns the
        existing row instead of sending again.
        """
        row = EmailDeliveryRow(
            delivery_id=delivery_id,
            report_id=report_id,
            incident_id=incident_id,
            recipients=recipients,
            subject=subject,
            status="pending",
            error=None,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            return None
        return _delivery_to_dict(row)

    def finalize(self, delivery_id: str, *, status: str, error: str | None) -> dict[str, Any]:
        """Record the send outcome on a previously reserved delivery."""
        row = self._session.get(EmailDeliveryRow, delivery_id)
        if row is None:  # pragma: no cover - reserve always precedes finalize
            raise LookupError(delivery_id)
        row.status = status
        row.error = error
        self._session.commit()
        return _delivery_to_dict(row)

    def list_for_report(self, report_id: str) -> list[dict[str, Any]]:
        rows = self._session.scalars(
            select(EmailDeliveryRow)
            .where(EmailDeliveryRow.report_id == report_id)
            .order_by(desc(EmailDeliveryRow.created_at), desc(EmailDeliveryRow.delivery_id))
        ).all()
        return [_delivery_to_dict(row) for row in rows]


def _delivery_to_dict(row: EmailDeliveryRow) -> dict[str, Any]:
    return {
        "delivery_id": row.delivery_id,
        "report_id": row.report_id,
        "recipients": list(row.recipients),
        "subject": row.subject,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
    }


def _canonical(value: Any) -> str:
    if isinstance(value, dict):
        return f"{value.get('namespace')}/{value.get('kind')}/{value.get('name')}"
    return str(value)
