"""Deterministic Alertmanager normalization and incident ingestion."""

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.contracts import (
    Alert,
    AlertmanagerAlertPayload,
    AlertSource,
    AlertStatus,
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.storage.models import AlertRow, IncidentRow
from packages.storage.repositories import IncidentEventRepository, IncidentRepository


def fingerprint_for_alert(payload: AlertmanagerAlertPayload) -> str:
    """Hash stable alert identity fields in canonical order."""
    labels = payload.labels
    identity = {
        "alert_name": labels.get("alertname", "unknown"),
        "service": labels.get("service", "unknown"),
        "namespace": labels.get("namespace", "unknown"),
        "cluster": labels.get("cluster", "unknown"),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_alert(payload: AlertmanagerAlertPayload) -> Alert:
    """Normalize Alertmanager casing and labels into the domain contract."""
    status = payload.status.upper()
    if status not in {AlertStatus.FIRING.value, AlertStatus.RESOLVED.value}:
        raise ValueError(f"unsupported alert status: {payload.status}")
    labels = payload.labels
    return Alert(
        alert_name=labels.get("alertname", "unknown"),
        service=labels.get("service", "unknown"),
        namespace=labels.get("namespace", "unknown"),
        cluster=labels.get("cluster", "unknown"),
        starts_at=payload.starts_at,
        # Alertmanager sends a zero/sentinel ``endsAt`` for firing alerts.
        # It is not an observation boundary; retaining it would make a live
        # alert appear to end before it starts and corrupt downstream windows.
        ends_at=payload.ends_at if status == AlertStatus.RESOLVED.value else None,
        labels=labels,
        annotations=payload.annotations,
        fingerprint=payload.fingerprint or fingerprint_for_alert(payload),
        status=AlertStatus(status),
        source=AlertSource.ALERTMANAGER,
    )


def _severity(alert: Alert) -> IncidentSeverity:
    value = alert.labels.get("severity", "warning").upper()
    return {
        "CRITICAL": IncidentSeverity.CRITICAL,
        "WARNING": IncidentSeverity.WARNING,
        "INFO": IncidentSeverity.INFO,
    }.get(value, IncidentSeverity.WARNING)


class IncidentManager:
    """Attach alerts to stable, idempotent incident episodes.

    A fingerprint is reusable. The ``starts_at`` value identifies one
    Alertmanager occurrence; once that occurrence is resolved, a later firing
    creates a new incident rather than reopening the historical episode.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def ingest(self, alert: Alert, *, now: datetime) -> Incident:
        """Create or update one occurrence without reopening terminal state."""
        rows = self._session.scalars(
            select(AlertRow)
            .where(
                AlertRow.fingerprint == alert.fingerprint,
                AlertRow.starts_at == alert.starts_at,
            )
            .order_by(AlertRow.alert_id.desc())
        ).all()
        active = next((item for item in rows if item.status == AlertStatus.FIRING.value), None)
        row = (
            active if alert.status is AlertStatus.FIRING else active or (rows[0] if rows else None)
        )
        if row is None or (alert.status is AlertStatus.FIRING and row.status != AlertStatus.FIRING):
            incident = Incident(
                incident_id=uuid4(),
                status=IncidentStatus.OPEN,
                severity=_severity(alert),
                source=IncidentSource.ALERTMANAGER,
                title=alert.alert_name,
                description=alert.annotations.get("description"),
                created_at=now,
                updated_at=now,
            )
            IncidentRepository(self._session).create(incident)
            self._session.add(
                AlertRow(
                    alert_id=alert.alert_id,
                    incident_id=incident.incident_id,
                    alert_name=alert.alert_name,
                    service=alert.service,
                    namespace=alert.namespace,
                    cluster=alert.cluster,
                    starts_at=alert.starts_at,
                    ends_at=alert.ends_at,
                    labels=alert.labels,
                    annotations=alert.annotations,
                    fingerprint=alert.fingerprint,
                    status=alert.status.value,
                    source=alert.source.value,
                )
            )
            self._session.commit()
            return incident

        existing_incident = IncidentRepository(self._session).get(row.incident_id)
        if existing_incident is None:
            raise LookupError(f"incident {row.incident_id} was not found")
        if row.status == alert.status.value and alert.status is AlertStatus.RESOLVED:
            # Alertmanager retries a resolved delivery; no new timeline event
            # or state mutation is needed for an already terminal occurrence.
            return existing_incident
        unchanged = (
            row.status == alert.status.value
            and row.ends_at == alert.ends_at
            and row.labels == alert.labels
            and row.annotations == alert.annotations
        )
        if unchanged and alert.status is AlertStatus.FIRING:
            return existing_incident
        row.ends_at = alert.ends_at
        row.status = alert.status.value
        row.labels = alert.labels
        row.annotations = alert.annotations
        if alert.status is AlertStatus.RESOLVED:
            incident_row = self._session.get(IncidentRow, existing_incident.incident_id)
            if incident_row is None:
                raise LookupError(f"incident {existing_incident.incident_id} was not found")
            incident_row.status = IncidentStatus.RESOLVED.value
            incident_row.updated_at = now
        event_type = (
            IncidentEventType.ALERT_RESOLVED
            if alert.status is AlertStatus.RESOLVED
            else IncidentEventType.ALERT_UPDATED
        )
        event = IncidentEvent(
            incident_id=existing_incident.incident_id,
            event_type=event_type,
            timestamp=now,
            correlation_id=existing_incident.correlation_id,
            payload={"fingerprint": alert.fingerprint, "status": alert.status.value},
        )
        IncidentEventRepository(self._session).append(event)
        return existing_incident
