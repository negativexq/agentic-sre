"""Repositories with explicit transaction boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select
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
from packages.rca.model import CLUSTER_SCOPE, JournalEntry, Lifecycle
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    DiagnosisRow,
    EventVersionRow,
    EvidenceRow,
    IncidentEventRow,
    IncidentRow,
    ObjectVersionRow,
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


class EventRepository:
    """Append-only journal of observed Kubernetes events.

    A coalesced repeat (Kubernetes bumps ``count``/``lastTimestamp`` on the
    same event object instead of creating a new one) is stored again under a
    new dedup key, mirroring how the object journal treats each observed
    version; an exact repeat is skipped.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, body: dict[str, Any], observed_at: datetime) -> bool:
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
        self._session.add(
            EventVersionRow(
                namespace=namespace,
                involved_kind=kind,
                involved_name=name,
                dedup_key=key,
                event_at=event_at,
                observed_at=observed_at,
                body=body,
            )
        )
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
        rows = self._session.scalars(
            select(EventVersionRow)
            .where(
                EventVersionRow.namespace.in_(namespaces),
                EventVersionRow.observed_at <= ends_at,
                (EventVersionRow.event_at >= starts_at)
                | (EventVersionRow.observed_at >= starts_at),
            )
            .order_by(EventVersionRow.observed_at, EventVersionRow.version_id)
        ).all()
        return [dict(row.body) for row in rows]

    def analysis_view(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Return the latest visible state of each logical Kubernetes Event.

        The append-only journal is deliberately kept separate from this view:
        coalesced Kubernetes updates remain available for provenance, while
        RCA receives one state per stable Event identity at the frozen cutoff.
        """
        rows = self._session.scalars(
            select(EventVersionRow)
            .where(
                EventVersionRow.namespace.in_(namespaces),
                EventVersionRow.observed_at <= ends_at,
                (EventVersionRow.event_at >= starts_at)
                | (EventVersionRow.observed_at >= starts_at),
            )
            .order_by(EventVersionRow.observed_at, EventVersionRow.version_id)
        ).all()
        latest: dict[str, EventVersionRow] = {}
        for row in rows:
            identity = event_identity(row.body, row.namespace)
            latest[identity] = row
        return [dict(row.body) for row in sorted(latest.values(), key=lambda item: item.version_id)]

    def history(
        self, *, namespaces: set[str], starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, Any]]:
        """Compatibility name for the deduplicated Event analysis/replay view."""
        return self.analysis_view(namespaces=namespaces, starts_at=starts_at, ends_at=ends_at)


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

    def record(self, body: dict[str, Any], observed_at: datetime) -> bool:
        """Store ``body`` unless it equals the object's latest live version."""
        metadata = child(body, "metadata")
        kind, name = body.get("kind"), metadata.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            raise ValueError("object body needs kind and metadata.name")
        namespace = str(metadata.get("namespace") or CLUSTER_SCOPE)
        key = f"{namespace}/{kind}/{name}"
        digest = self.content_hash(body)
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
        elif latest.content_hash == digest:
            return False
        else:
            lifecycle = Lifecycle.UPDATED
        self._session.add(
            ObjectVersionRow(
                object_key=key,
                namespace=namespace,
                kind=kind,
                name=name,
                observed_at=observed_at,
                content_hash=digest,
                body=body,
                lifecycle=lifecycle.value,
            )
        )
        self._session.commit()
        return True

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
        rows = self._session.scalars(
            select(ObjectVersionRow)
            .where(
                ObjectVersionRow.namespace.in_(namespaces | {CLUSTER_SCOPE}),
                ObjectVersionRow.observed_at <= ends_at,
            )
            .order_by(ObjectVersionRow.observed_at, ObjectVersionRow.version_id)
        ).all()
        baseline: dict[str, ObjectVersionRow] = {}
        selected: list[ObjectVersionRow] = []
        for row in rows:
            if row.observed_at < starts_at:
                baseline[row.object_key] = row
            else:
                selected.append(row)
        # Objects already deleted before the window are not part of the incident.
        before = [row for row in baseline.values() if row.lifecycle != Lifecycle.DELETED]
        ordered = sorted([*before, *selected], key=lambda row: (row.observed_at, row.version_id))
        return [
            JournalEntry(
                object_key=row.object_key,
                observed_at=row.observed_at,
                body=row.body,
                version_id=row.version_id,
                lifecycle=Lifecycle(row.lifecycle),
            )
            for row in ordered
        ]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
