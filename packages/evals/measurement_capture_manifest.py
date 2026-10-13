"""Freeze a DEV capture in a new SQLite database through the real manifest builder.

Capture-only: no DiagnosisRow, RCA execution or claim authority is manufactured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from packages.evals.frozen_request_binding import FrozenLog, FrozenRequestEvidence, FrozenSpan

# Initialize the investigation package before storage.manifest's provider import.
from packages.rca.investigation import tempo as _tempo  # noqa: F401
from packages.rca.manifest import manifest_membership_digest
from packages.storage.manifest import ManifestRequest, build_manifest, load_manifest, load_members
from packages.storage.models import Base, IncidentRow, LogObservationRow, TraceObservationRow
from packages.storage.repositories import LogObservationRepository, TraceObservationRepository


def freeze_measurement_capture(
    path: Path, evidence: FrozenRequestEvidence
) -> tuple[FrozenRequestEvidence, str]:
    if path.exists():
        raise ValueError("Measurement database must be a new file")
    engine = create_engine(f"sqlite:///{path.resolve()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    incident = uuid4()
    correlation = uuid4()
    try:
        with factory() as session:
            session.add(
                IncidentRow(
                    incident_id=incident,
                    status="OPEN",
                    severity="LOW",
                    source="MEASUREMENT",
                    title="DEV capture only; RCA not evaluated",
                    created_at=evidence.episode_start,
                    updated_at=evidence.frozen_cutoff,
                    correlation_id=correlation,
                )
            )
            session.commit()
            LogObservationRepository(session).record(
                incident,
                [item.record for item in evidence.logs],
                evidence.frozen_cutoff,
                source_system="loki",
            )
            TraceObservationRepository(session).record(
                incident, [item.record for item in evidence.spans], evidence.frozen_cutoff
            )
            session.commit()
        request = ManifestRequest(
            run_id=evidence.run_id,
            incident_id=incident,
            correlation_id=correlation,
            starts_at=evidence.episode_start,
            ends_at=evidence.frozen_cutoff,
            window_end=evidence.frozen_cutoff,
            namespaces=frozenset(),
            journal_namespaces=frozenset(),
            snapshot_cycle_id=None,
            listed_objects=0,
            provider_capabilities=("logs", "runtime_traces"),
        )
        entries = build_manifest(factory, request, timestamp=evidence.frozen_cutoff)
        digest = manifest_membership_digest((item.source_type, item.source_id) for item in entries)
        # Re-open query-only and reload exact members; no time-window repair.
        with engine.connect() as connection:
            connection.exec_driver_sql("pragma query_only=on")
            with Session(bind=connection) as session:
                loaded_entries = load_manifest(session, evidence.run_id)
                if digest != manifest_membership_digest(
                    (item.source_type, item.source_id) for item in loaded_entries
                ):
                    raise ValueError("Capture manifest membership changed during round-trip")
                members = load_members(session, loaded_entries)
                log_ids = [int(e.source_id) for e in loaded_entries if e.source_type == "LOG"]
                trace_ids = [int(e.source_id) for e in loaded_entries if e.source_type == "TRACE"]
                log_refs = {
                    r.evidence_id: r.observation_id
                    for r in session.execute(
                        select(
                            LogObservationRow.observation_id, LogObservationRow.evidence_id
                        ).where(LogObservationRow.observation_id.in_(log_ids))
                    )
                }
                trace_refs = {
                    r.span["evidence_id"]: r.observation_id
                    for r in session.execute(
                        select(TraceObservationRow.observation_id, TraceObservationRow.span).where(
                            TraceObservationRow.observation_id.in_(trace_ids)
                        )
                    )
                }
                if len(log_refs) != len(log_ids) or len(trace_refs) != len(trace_ids):
                    raise ValueError("Capture evidence IDs do not identify unique manifest rows")
        if sorted(item.record.model_dump_json() for item in evidence.logs) != sorted(
            item.model_dump_json() for item in members.logs
        ):
            raise ValueError("Log records changed or disappeared in capture manifest")
        if sorted(item.record.model_dump_json() for item in evidence.spans) != sorted(
            item.model_dump_json() for item in members.traces
        ):
            raise ValueError("Trace records changed or disappeared in capture manifest")
        provenance: dict[str, Any] = {
            "incident_id": str(incident),
            "run_id": evidence.run_id,
            "captured_at": evidence.frozen_cutoff,
        }
        loaded = evidence.model_copy(
            update={
                "incident_id": str(incident),
                "logs": tuple(
                    FrozenLog(
                        **provenance,
                        source_reference=f"capture-manifest:{evidence.run_id}:LOG:{log_refs[record.evidence_id]}",
                        record=record,
                    )
                    for record in members.logs
                ),
                "spans": tuple(
                    FrozenSpan(
                        **provenance,
                        source_reference=f"capture-manifest:{evidence.run_id}:TRACE:{trace_refs[record.evidence_id]}",
                        record=record,
                    )
                    for record in members.traces
                ),
            }
        )
        return loaded, digest
    finally:
        engine.dispose()
