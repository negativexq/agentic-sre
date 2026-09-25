"""Build and load a diagnosis run's base evidence manifest.

The manifest is written once, after capture has committed: one transaction
selects the exact persistent ids of every authoritative row in the run's final
window, inserts them as ``run_evidence_manifest`` rows and inserts the run's
``EVIDENCE_GATHERED`` boundary event (window end, snapshot cycle) with them.
On PostgreSQL that transaction is REPEATABLE READ from its first query, so a
row committed elsewhere after the snapshot is taken is not part of the run.
SQLite runs the same single transaction without claiming MVCC semantics.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.contracts import IncidentEvent, IncidentEventType
from packages.rca.manifest import ManifestEntry, ordered_entries
from packages.rca.model import JournalEntry, LogRecord
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    LifecycleObservationRow,
    RunEvidenceManifestRow,
)
from packages.storage.repositories import (
    AlertRepository,
    EventRepository,
    IncidentEventRepository,
    LifecycleRecord,
    LogObservationRepository,
    ObjectVersionRepository,
    PersistedSnapshotCycle,
    SnapshotCycleRepository,
    _lifecycle_record,
)


class ManifestAlertPayloadMissing(LookupError):
    """A manifest ALERT entry has no frozen content (taken before M19-3.6a).

    Such a run is not replayable: its alerts may have changed since, and the
    current ``alerts`` row is never used in their place.
    """


@dataclass(frozen=True)
class ManifestRequest:
    """Everything that defines one run's epistemic boundary."""

    run_id: str
    incident_id: UUID
    correlation_id: UUID
    starts_at: datetime
    ends_at: datetime
    window_end: datetime
    namespaces: frozenset[str]
    journal_namespaces: frozenset[str]
    snapshot_cycle_id: int | None
    listed_objects: int


@dataclass(frozen=True)
class ManifestMembers:
    """The persisted evidence a run may use, loaded only by its manifest ids."""

    # Alert content exactly as the manifest froze it (see ``alert_payload``).
    alerts: tuple[dict[str, Any], ...]
    journal: tuple[JournalEntry, ...]
    events: tuple[tuple[int, dict[str, Any]], ...]
    lifecycle: tuple[LifecycleRecord, ...]
    logs: tuple[LogRecord, ...]
    snapshot: PersistedSnapshotCycle | None


def alert_payload(row: AlertRow) -> dict[str, Any]:
    """The alert content a run knows: frozen when the manifest is taken."""
    return {
        "alert_name": row.alert_name,
        "service": row.service,
        "namespace": row.namespace,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat() if row.ends_at is not None else None,
        "status": row.status,
        "labels": dict(sorted(row.labels.items())),
        "fingerprint": row.fingerprint,
    }


def _entries(source_type: str, ids: Sequence[object]) -> list[ManifestEntry]:
    return [ManifestEntry(source_type, str(item)) for item in ids]


def select_members(session: Session, request: ManifestRequest) -> list[ManifestEntry]:
    """Exact ids of every authoritative row in the request's window."""
    window = {"starts_at": request.starts_at, "ends_at": request.ends_at}
    journal_namespaces = set(request.journal_namespaces)
    # Alert rows are updated in place on resolve, so their content is frozen here.
    alert_ids = AlertRepository(session).ids_for_incident(request.incident_id)
    entries = [
        ManifestEntry("ALERT", str(row.alert_id), alert_payload(row))
        for row in (
            session.scalars(select(AlertRow).where(AlertRow.alert_id.in_(alert_ids)))
            if alert_ids
            else []
        )
    ]
    if request.snapshot_cycle_id is not None:
        entries += _entries("SNAPSHOT_CYCLE", [request.snapshot_cycle_id])
    entries += _entries(
        "OBJECT_VERSION",
        ObjectVersionRepository(session).history_version_ids(
            namespaces=journal_namespaces, **window
        ),
    )
    entries += _entries(
        "EVENT_VERSION",
        EventRepository(session).analysis_version_ids(namespaces=journal_namespaces, **window),
    )
    entries += _entries(
        "LIFECYCLE",
        session.scalars(
            select(LifecycleObservationRow.observation_id).where(
                LifecycleObservationRow.namespace.in_(request.namespaces),
                LifecycleObservationRow.observed_at >= request.starts_at,
                LifecycleObservationRow.observed_at <= request.ends_at,
            )
        ).all(),
    )
    # Provenance member of the base universe; RCA has no change-record consumer.
    entries += _entries(
        "CHANGE",
        session.scalars(
            select(ChangeRecordRow.change_id).where(
                ChangeRecordRow.timestamp >= request.starts_at,
                ChangeRecordRow.timestamp <= request.ends_at,
            )
        ).all(),
    )
    entries += _entries(
        "LOG",
        LogObservationRepository(session).incident_observation_ids(
            incident_id=request.incident_id, **window
        ),
    )
    return entries


def build_manifest(
    session_factory: sessionmaker[Session], request: ManifestRequest, *, timestamp: datetime
) -> tuple[ManifestEntry, ...]:
    """Select, persist and commit the run's manifest and boundary event together."""
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            # Must be set before the transaction's first query takes its snapshot.
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        entries = ordered_entries(select_members(session, request))
        for sequence, entry in enumerate(entries, start=1):
            session.add(
                RunEvidenceManifestRow(
                    run_id=request.run_id,
                    sequence=sequence,
                    source_type=entry.source_type,
                    source_id=entry.source_id,
                    payload=dict(entry.payload) if entry.payload is not None else None,
                )
            )
        counts = Counter(entry.source_type for entry in entries)
        IncidentEventRepository(session).append(
            IncidentEvent(
                incident_id=request.incident_id,
                event_type=IncidentEventType.EVIDENCE_GATHERED,
                timestamp=timestamp,
                correlation_id=request.correlation_id,
                payload={
                    "run_id": request.run_id,
                    "window_end": request.window_end.isoformat(),
                    "snapshot_cycle_id": request.snapshot_cycle_id,
                    "objects": request.listed_objects,
                    "journal": counts["OBJECT_VERSION"],
                    "events": counts["EVENT_VERSION"],
                    "logs": counts["LOG"],
                },
            ),
            commit=False,
        )
        session.commit()
    return entries


def load_manifest(session: Session, run_id: str) -> tuple[ManifestEntry, ...]:
    """A run's manifest entries in their persisted sequence."""
    rows = session.scalars(
        select(RunEvidenceManifestRow)
        .where(RunEvidenceManifestRow.run_id == run_id)
        .order_by(RunEvidenceManifestRow.sequence)
    ).all()
    return tuple(ManifestEntry(row.source_type, row.source_id, row.payload) for row in rows)


def load_members(session: Session, entries: Sequence[ManifestEntry]) -> ManifestMembers:
    """Load exactly the manifest's members; nothing is queried by time window."""
    ids: dict[str, list[str]] = {}
    for entry in entries:
        ids.setdefault(entry.source_type, []).append(entry.source_id)

    def ints(source_type: str) -> list[int]:
        return [int(item) for item in ids.get(source_type, [])]

    lifecycle_ids = ints("LIFECYCLE")
    lifecycle_rows = (
        session.scalars(
            select(LifecycleObservationRow)
            .where(LifecycleObservationRow.observation_id.in_(lifecycle_ids))
            .order_by(LifecycleObservationRow.observed_at, LifecycleObservationRow.observation_id)
        ).all()
        if lifecycle_ids
        else []
    )
    cycles = ints("SNAPSHOT_CYCLE")
    alerts: list[dict[str, Any]] = []
    for entry in entries:
        if entry.source_type != "ALERT":
            continue
        if entry.payload is None:
            raise ManifestAlertPayloadMissing(
                f"manifest ALERT {entry.source_id} has no frozen content; "
                "the run cannot be rebuilt from its manifest"
            )
        alerts.append(dict(entry.payload))
    return ManifestMembers(
        alerts=tuple(sorted(alerts, key=lambda item: (item["starts_at"], item["alert_name"]))),
        journal=tuple(ObjectVersionRepository(session).entries(ints("OBJECT_VERSION"))),
        events=tuple(EventRepository(session).bodies(ints("EVENT_VERSION"))),
        lifecycle=tuple(_lifecycle_record(row) for row in lifecycle_rows),
        logs=tuple(LogObservationRepository(session).records(ints("LOG"))),
        snapshot=SnapshotCycleRepository(session).load(cycles[0]) if cycles else None,
    )


__all__ = [
    "ManifestAlertPayloadMissing",
    "ManifestMembers",
    "alert_payload",
    "ManifestRequest",
    "build_manifest",
    "load_manifest",
    "load_members",
    "select_members",
]
