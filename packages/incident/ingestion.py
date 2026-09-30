"""Deterministic Alertmanager normalization and incident ingestion."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


@dataclass(frozen=True)
class IngestOutcome:
    incident: Incident
    refired: bool = False  # the occurrence continued an existing episode (``ALERT_REFIRED``)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class IncidentManager:
    """Attach alerts to stable, idempotent incident episodes.

    A fingerprint is reusable. The ``starts_at`` value identifies one
    Alertmanager occurrence. With ``quiet`` of zero (the product default), a later
    firing after a resolution creates a new incident. With a positive ``quiet``, a
    new occurrence that fires again within it, or while an earlier one still fires,
    continues that episode (``docs/architecture/incident-episode-contract.md``).
    """

    def __init__(self, session: Session, *, quiet: timedelta = timedelta(0)) -> None:
        self._session = session
        self._quiet = quiet

    def ingest(self, alert: Alert, *, now: datetime) -> Incident:
        """Create or update one occurrence without reopening terminal state."""
        return self.ingest_occurrence(alert, now=now).incident

    def ingest_occurrence(self, alert: Alert, *, now: datetime) -> IngestOutcome:
        """``ingest`` that also says whether the occurrence continued an episode."""
        if self._quiet > timedelta(0) and alert.status is AlertStatus.FIRING:
            continued = self._continue_episode(alert, now=now)
            if continued is not None:
                return IngestOutcome(continued, refired=True)
        return IngestOutcome(self._ingest(alert, now=now))

    def _continue_episode(self, alert: Alert, *, now: datetime) -> Incident | None:
        """Attach a new occurrence to its fingerprint's recent episode, keeping the history."""
        known = self._session.scalar(
            select(AlertRow.alert_id).where(
                AlertRow.fingerprint == alert.fingerprint, AlertRow.starts_at == alert.starts_at
            )
        )
        if known is not None:
            return None
        earlier = self._session.scalar(
            select(AlertRow)
            .where(AlertRow.fingerprint == alert.fingerprint, AlertRow.starts_at < alert.starts_at)
            .order_by(AlertRow.starts_at.desc())
            .limit(1)
        )
        if earlier is None:
            return None
        gap: timedelta | None = None
        if earlier.status != AlertStatus.FIRING.value:
            if earlier.ends_at is None:
                return None
            gap = _utc(alert.starts_at) - _utc(earlier.ends_at)
            if gap >= self._quiet:
                return None
        incident_row = self._session.get(IncidentRow, earlier.incident_id)
        if incident_row is None:
            return None
        try:
            self._session.add(
                AlertRow(
                    alert_id=alert.alert_id,
                    incident_id=incident_row.incident_id,
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
            incident_row.status = IncidentStatus.OPEN.value
            incident_row.updated_at = now
            self._session.commit()
        except IntegrityError:
            # a concurrent delivery stored this occurrence first; the ordinary path returns it
            self._session.rollback()
            return None
        incident = IncidentRepository(self._session).get(incident_row.incident_id)
        if incident is None:
            raise LookupError(f"incident {incident_row.incident_id} was not found")
        # Appended, never rewritten: the earlier ALERT_RESOLVED stays before this event.
        IncidentEventRepository(self._session).append(
            IncidentEvent(
                incident_id=incident.incident_id,
                event_type=IncidentEventType.ALERT_REFIRED,
                timestamp=now,
                correlation_id=incident.correlation_id,
                payload={
                    "fingerprint": alert.fingerprint,
                    "status": IncidentStatus.OPEN.value,
                    "starts_at": _utc(alert.starts_at).isoformat(),
                    "previous_starts_at": _utc(earlier.starts_at).isoformat(),
                    "gap_seconds": None if gap is None else gap.total_seconds(),
                },
            )
        )
        return incident

    def _ingest(self, alert: Alert, *, now: datetime) -> Incident:
        row = self._session.scalar(
            select(AlertRow).where(
                AlertRow.fingerprint == alert.fingerprint,
                AlertRow.starts_at == alert.starts_at,
            )
        )
        if row is None:
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
            try:
                # Keep the parent and occurrence in one transaction. If a
                # concurrent delivery wins the unique occurrence constraint,
                # this transaction rolls back both rows and then returns the
                # canonical winner below.
                IncidentRepository(self._session).create(incident, commit=False)
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
            except IntegrityError:
                self._session.rollback()
                row = self._session.scalar(
                    select(AlertRow).where(
                        AlertRow.fingerprint == alert.fingerprint,
                        AlertRow.starts_at == alert.starts_at,
                    )
                )
                if row is None:
                    raise

        existing_incident = IncidentRepository(self._session).get(row.incident_id)
        if existing_incident is None:
            raise LookupError(f"incident {row.incident_id} was not found")
        if row.status == alert.status.value and alert.status is AlertStatus.RESOLVED:
            # Alertmanager retries a resolved delivery; no new timeline event
            # or state mutation is needed for an already terminal occurrence.
            return existing_incident
        # A late/retried firing for an already resolved occurrence is still
        # the same historical occurrence. Never reopen it and never create a
        # second incident with the same uniqueness key.
        if alert.status is AlertStatus.FIRING and row.status != AlertStatus.FIRING:
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
            still_firing = self._session.scalar(
                select(AlertRow.alert_id).where(
                    AlertRow.incident_id == existing_incident.incident_id,
                    AlertRow.alert_id != row.alert_id,
                    AlertRow.status == AlertStatus.FIRING.value,
                )
            )
            if still_firing is None:  # an episode resolves when none of its occurrences fires
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
