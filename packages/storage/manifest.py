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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.contracts import IncidentEvent, IncidentEventType
from packages.rca.alert_coverage import (
    ALERT_COVERAGE_SOURCE,
    AlertCoverageBoundary,
    AlertCoverageBoundaryError,
    AlertCoverageConfig,
)
from packages.rca.manifest import ManifestEntry, manifest_membership_digest, ordered_entries
from packages.rca.model import JournalEntry, LogRecord, TraceSpanObservation
from packages.rca.provider_adapter import PROVIDER_CAPABILITIES, ProviderIntegrityError
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    IncidentEventRow,
    LifecycleObservationRow,
    RunEvidenceManifestRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import (
    AlertCoverageRepository,
    AlertRepository,
    EventRepository,
    IncidentEventRepository,
    LifecycleRecord,
    LogObservationRepository,
    ObjectVersionRepository,
    PersistedSnapshotCycle,
    SnapshotCycleRepository,
    TraceObservationRepository,
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
    # The run's configured provider capability contract (``ProviderAdapter.capabilities``).
    provider_capabilities: tuple[str, ...]
    # How a polling gap breaks alert-channel coverage (M21 contract §10.2).
    alert_coverage_config: AlertCoverageConfig = field(default_factory=AlertCoverageConfig)


def canonical_provider_capabilities(names: Sequence[str]) -> list[str]:
    """Sorted unique capability names; an unknown name is an error, never dropped."""
    unknown = sorted(set(names) - set(PROVIDER_CAPABILITIES))
    if unknown:
        raise ValueError(f"unknown provider capabilities: {unknown}")
    return sorted(set(names))


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
    # spans captured for the incident (live-trace-design.md §3); empty before trace capture existed
    traces: tuple[TraceSpanObservation, ...] = ()


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
    entries += _entries(
        "TRACE",
        TraceObservationRepository(session).incident_observation_ids(
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
        # Frozen with the membership, in the same transaction: the alert channel's
        # coverage at the boundary is evidence, never recomputed later.
        alert_coverage = AlertCoverageRepository(session).boundary_at(
            source=ALERT_COVERAGE_SOURCE,
            at=request.window_end,
            config=request.alert_coverage_config,
        )
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
                    "traces": counts["TRACE"],
                    "provider_capabilities": canonical_provider_capabilities(
                        request.provider_capabilities
                    ),
                    "alert_coverage": alert_coverage.to_payload(),
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


def load_manifest_digest(session: Session, run_id: str) -> str:
    """Reload a run's persisted membership and compute its canonical digest."""
    entries = load_manifest(session, run_id)
    return manifest_membership_digest((entry.source_type, entry.source_id) for entry in entries)


class ReplayDataError(LookupError):
    """A run's persisted replay state is missing, malformed or inconsistent.

    Replay never repairs it: no clock, newest cycle or current row stands in.
    """


class ReplayTapeCorrupt(ReplayDataError, ProviderIntegrityError):
    """A recorded tape row cannot be served as its typed result.

    Raised inside provider reads during replay, so it must propagate through
    tool wrappers instead of becoming a tool-error observation.
    """


class ReplayProviderCapabilitiesMissing(ReplayDataError):
    """The run's boundary predates M19-3.14a and never recorded its provider capabilities.

    Such a run is not replayable: which providers it could read is part of its
    epistemic environment, and it is never inferred from the tape.
    """


@dataclass(frozen=True)
class RunBoundary:
    """A run's persisted ``EVIDENCE_GATHERED`` boundary, exactly as written."""

    run_id: str
    incident_id: UUID
    window_end: datetime
    snapshot_cycle_id: int | None
    provider_capabilities: tuple[str, ...]
    # How many objects the run's snapshot cycle listed (0 without a cycle).
    listed_objects: int
    # The alert-channel coverage frozen at the boundary; None for runs recorded
    # before M21 amendment 4, which never captured it.
    alert_coverage: AlertCoverageBoundary | None = None


def load_run_boundary(session: Session, run_id: str) -> RunBoundary:
    """The run's single ``EVIDENCE_GATHERED`` event; anything else is an error."""
    rows = [
        row
        for row in session.scalars(
            select(IncidentEventRow).where(
                IncidentEventRow.event_type == IncidentEventType.EVIDENCE_GATHERED.value
            )
        )
        if row.payload.get("run_id") == run_id
    ]
    if len(rows) != 1:
        raise ReplayDataError(f"run {run_id} has {len(rows)} EVIDENCE_GATHERED boundaries")
    payload = rows[0].payload
    raw_end = payload.get("window_end")
    try:
        window_end = datetime.fromisoformat(raw_end) if isinstance(raw_end, str) else None
    except ValueError:
        window_end = None
    if window_end is None or window_end.utcoffset() is None:
        raise ReplayDataError(f"run {run_id} boundary has no valid window_end: {raw_end!r}")
    # A resolved incident's run captures no cycle and records null; an absent
    # key or a non-integer is malformed.
    if "snapshot_cycle_id" not in payload:
        raise ReplayDataError(f"run {run_id} boundary has no snapshot_cycle_id")
    cycle_id = payload["snapshot_cycle_id"]
    if cycle_id is not None and (not isinstance(cycle_id, int) or isinstance(cycle_id, bool)):
        raise ReplayDataError(f"run {run_id} boundary snapshot_cycle_id is {cycle_id!r}")
    if "provider_capabilities" not in payload:
        raise ReplayProviderCapabilitiesMissing(
            f"run {run_id} boundary has no provider_capabilities; it is not replayable"
        )
    capabilities = payload["provider_capabilities"]
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ReplayDataError(f"run {run_id} provider_capabilities is {capabilities!r}")
    try:
        canonical = canonical_provider_capabilities(capabilities)
    except ValueError as error:
        raise ReplayDataError(f"run {run_id} boundary: {error}") from error
    if canonical != capabilities:
        raise ReplayDataError(
            f"run {run_id} provider_capabilities {capabilities!r} are not sorted and unique"
        )
    listed = payload.get("objects")
    if not isinstance(listed, int) or isinstance(listed, bool) or listed < 0:
        raise ReplayDataError(f"run {run_id} boundary listed object count is {listed!r}")
    alert_coverage = None
    if "alert_coverage" in payload:
        try:
            alert_coverage = AlertCoverageBoundary.from_payload(payload["alert_coverage"])
        except AlertCoverageBoundaryError as error:
            raise ReplayDataError(f"run {run_id} boundary: {error}") from error
    return RunBoundary(
        run_id,
        rows[0].incident_id,
        window_end,
        cycle_id,
        tuple(canonical),
        listed,
        alert_coverage,
    )


def load_replay_run(session: Session, run_id: str) -> tuple[RunBoundary, ManifestMembers]:
    """A run's boundary and exactly its manifest members, checked for consistency.

    The boundary's snapshot cycle must be the manifest's only SNAPSHOT_CYCLE
    member and belong to the run; every manifest member row must still exist.
    """
    boundary = load_run_boundary(session, run_id)
    entries = load_manifest(session, run_id)
    manifest_cycles = [int(e.source_id) for e in entries if e.source_type == "SNAPSHOT_CYCLE"]
    expected_cycles = [] if boundary.snapshot_cycle_id is None else [boundary.snapshot_cycle_id]
    if manifest_cycles != expected_cycles:
        raise ReplayDataError(
            f"run {run_id} boundary snapshot cycle {boundary.snapshot_cycle_id} "
            f"disagrees with manifest cycles {manifest_cycles}"
        )
    if boundary.snapshot_cycle_id is not None:
        cycle = session.get(SnapshotCycleRow, boundary.snapshot_cycle_id)
        if cycle is None:
            raise ReplayDataError(f"snapshot cycle {boundary.snapshot_cycle_id} was not found")
        if cycle.run_id != run_id:
            raise ReplayDataError(
                f"snapshot cycle {cycle.cycle_id} belongs to run {cycle.run_id}, not {run_id}"
            )
    members = load_members(session, entries)
    # A snapshot object row that disappeared would otherwise be silently omitted.
    snapshot_objects = len(members.snapshot.objects) if members.snapshot is not None else 0
    if snapshot_objects != boundary.listed_objects:
        raise ReplayDataError(
            f"run {run_id} boundary listed {boundary.listed_objects} snapshot object(s) "
            f"but {snapshot_objects} persisted row(s) remain"
        )
    wanted = Counter(entry.source_type for entry in entries)
    loaded = {
        "OBJECT_VERSION": len(members.journal),
        "EVENT_VERSION": len(members.events),
        "LIFECYCLE": len(members.lifecycle),
        "LOG": len(members.logs),
        "TRACE": len(members.traces),
    }
    for source_type, count in loaded.items():
        if count != wanted[source_type]:
            raise ReplayDataError(
                f"run {run_id} manifest lists {wanted[source_type]} {source_type} "
                f"member(s) but {count} persisted row(s) remain"
            )
    return boundary, members


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
        traces=tuple(TraceObservationRepository(session).spans(ints("TRACE"))),
    )


__all__ = [
    "ManifestAlertPayloadMissing",
    "ManifestMembers",
    "ReplayDataError",
    "ReplayProviderCapabilitiesMissing",
    "ReplayTapeCorrupt",
    "RunBoundary",
    "alert_payload",
    "ManifestRequest",
    "build_manifest",
    "canonical_provider_capabilities",
    "load_manifest",
    "load_manifest_digest",
    "load_members",
    "load_replay_run",
    "load_run_boundary",
    "select_members",
]
