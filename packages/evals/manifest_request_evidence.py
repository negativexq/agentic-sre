"""Exact frozen LOG/TRACE members from a read-only database transaction.

No latest-row/window fallback, schema change, provider read or RCA execution.
Legacy log schemas are reflected so missing attributes remain empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from packages.evals.frozen_request_binding import (
    FrozenLog,
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.rca.manifest import ManifestEntry, manifest_membership_digest
from packages.rca.model import LogRecord, TraceSpanObservation
from packages.storage.models import DiagnosisRow, IncidentEventRow, RunEvidenceManifestRow


class FrozenSourceIntegrityError(ValueError):
    """An invalid source is never replaced with current incident evidence."""


@dataclass(frozen=True)
class ManifestRequestEvidence:
    evidence: FrozenRequestEvidence
    manifest_digest: str
    log_attributes_column_present: bool
    recorded_engine_version: str | None
    revision_number: int
    recorded_resolution: str | None

    def report(self) -> dict[str, Any]:
        report = corroborate_frozen_requests(self.evidence).model_dump(mode="json")
        report["manifest_membership"] = "VERIFIED_AGAINST_RECORDED_DIGEST"
        report["verification_scope"] = "EXACT_DATABASE_MANIFEST_MEMBERS_AND_RECORD_CONSISTENCY"
        return {
            "report": report,
            "manifest_digest": self.manifest_digest,
            "record_integrity": "SCOPE_AND_TIMESTAMPS_CHECKED_CONTENT_NOT_AUTHENTICATED",
            "log_attributes_column_present": self.log_attributes_column_present,
            "recorded_engine_version": self.recorded_engine_version,
            "revision_number": self.revision_number,
            "recorded_resolution": self.recorded_resolution,
            "requested_scope": {
                "episode_start": self.evidence.episode_start.isoformat(),
                "episode_end": self.evidence.episode_end.isoformat(),
                "frozen_cutoff": self.evidence.frozen_cutoff.isoformat(),
                "caller_service": self.evidence.caller_service,
                "configured_peer": self.evidence.configured_peer,
                "basis": "EXPLICIT_ARGUMENTS_BOUNDED_BY_RECORDED_RUN_CUTOFF",
                "claim_episode_or_service_declaration_verified": False,
            },
        }


def _readonly(connection: Connection) -> None:
    dialect = connection.dialect.name
    if dialect == "postgresql":
        safe = connection.execute(text("show transaction_read_only")).scalar() == "on"
        isolation = connection.execute(text("show transaction_isolation")).scalar()
        if isolation not in {"repeatable read", "serializable"}:
            raise FrozenSourceIntegrityError(
                "PostgreSQL evidence reads require a repeatable snapshot"
            )
    elif dialect == "sqlite":
        safe = connection.execute(text("pragma query_only")).scalar() == 1
    else:
        safe = False
    if not safe:
        raise FrozenSourceIntegrityError(
            "A verified read-only PostgreSQL/SQLite transaction is required"
        )


def _utc(value: datetime | None) -> datetime | None:
    # Reflected SQLite/naive TIMESTAMP values use the storage UTCDateTime convention.
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def load_manifest_request_evidence(
    connection: Connection,
    *,
    run_id: str,
    episode_start: datetime,
    episode_end: datetime,
    caller_service: str,
    configured_peer: str,
    claim_id: str | None = None,
) -> ManifestRequestEvidence:
    _readonly(connection)
    with Session(bind=connection, autoflush=False) as session:
        boundaries = session.scalars(
            select(IncidentEventRow).where(
                IncidentEventRow.event_type == "EVIDENCE_GATHERED",
                IncidentEventRow.payload["run_id"].as_string() == run_id,
            )
        ).all()
        if len(boundaries) != 1:
            raise FrozenSourceIntegrityError("Run requires exactly one EVIDENCE_GATHERED boundary")
        boundary_incident = boundaries[0].incident_id
        try:
            window_end = datetime.fromisoformat(boundaries[0].payload["window_end"])
        except (KeyError, TypeError, ValueError) as error:
            raise FrozenSourceIntegrityError("Run boundary has no valid window_end") from error
        if window_end.utcoffset() is None:
            raise FrozenSourceIntegrityError("Run boundary window_end must be timezone-aware")
        entries = tuple(
            ManifestEntry(row.source_type, row.source_id, row.payload)
            for row in session.scalars(
                select(RunEvidenceManifestRow)
                .where(RunEvidenceManifestRow.run_id == run_id)
                .order_by(RunEvidenceManifestRow.sequence)
            )
        )
        diagnoses = session.execute(
            select(
                DiagnosisRow.incident_id,
                DiagnosisRow.manifest_digest,
                DiagnosisRow.engine_version,
                DiagnosisRow.revision_number,
                DiagnosisRow.document["resolution"].as_string().label("recorded_resolution"),
            ).where(DiagnosisRow.run_id == run_id)
        ).all()
    if len(diagnoses) != 1 or not diagnoses[0].manifest_digest:
        raise FrozenSourceIntegrityError(
            "Run requires exactly one diagnosis with a recorded manifest digest"
        )
    if diagnoses[0].incident_id != boundary_incident:
        raise FrozenSourceIntegrityError("Diagnosis and run boundary incident mismatch")
    membership = [(entry.source_type, entry.source_id) for entry in entries]
    digest = manifest_membership_digest(membership)
    if len(set(membership)) != len(membership) or digest != diagnoses[0].manifest_digest:
        raise FrozenSourceIntegrityError(
            "Manifest membership does not match the recorded diagnosis digest"
        )
    if (
        any(t.tzinfo is None or t.utcoffset() is None for t in (episode_start, episode_end))
        or not episode_start <= episode_end <= window_end
    ):
        raise FrozenSourceIntegrityError(
            "Explicit episode interval must be zoned and end within the frozen boundary"
        )
    metadata = MetaData()
    logs_table = Table("log_observations", metadata, autoload_with=connection)
    traces_table = Table("trace_observations", metadata, autoload_with=connection)
    has_attributes = "attributes" in logs_table.c
    logs: list[FrozenLog] = []
    spans: list[FrozenSpan] = []
    for source_type, table in (("LOG", logs_table), ("TRACE", traces_table)):
        raw_ids = [entry.source_id for entry in entries if entry.source_type == source_type]
        if len(raw_ids) > 10000 or any(
            not value.isascii() or not value.isdigit() or str(int(value)) != value or int(value) < 1
            for value in raw_ids
        ):
            raise FrozenSourceIntegrityError("Malformed or oversized LOG/TRACE manifest inventory")
        ids = [int(value) for value in raw_ids]
        rows = (
            connection.execute(
                select(table)
                .where(table.c.observation_id.in_(ids))
                .order_by(table.c.observation_id)
            )
            .mappings()
            .all()
            if ids
            else []
        )
        if len(rows) != len(ids):
            raise FrozenSourceIntegrityError(f"A frozen {source_type} member is missing")
        for row in rows:
            if UUID(str(row["incident_id"])) != boundary_incident:
                raise FrozenSourceIntegrityError(
                    f"A frozen {source_type} member belongs to another incident"
                )
            captured = _utc(row["observed_at"])
            event_at = _utc(row["event_at"])
            if captured is None or captured > window_end:
                raise FrozenSourceIntegrityError(
                    f"A frozen {source_type} member was captured after its boundary"
                )
            if event_at is not None and captured < event_at:
                raise FrozenSourceIntegrityError(
                    f"A frozen {source_type} capture precedes its event"
                )
            provenance: dict[str, Any] = {
                "incident_id": str(boundary_incident),
                "run_id": run_id,
                "source_reference": f"manifest:{run_id}:{source_type}:{row['observation_id']}",
                "captured_at": captured,
            }
            if source_type == "LOG":
                record = LogRecord(
                    service=row["service"],
                    at=event_at,
                    severity=row["severity"],
                    message=row["message"],
                    evidence_id=row["evidence_id"],
                    attributes=(row["attributes"] or {}) if has_attributes else {},
                )
                logs.append(FrozenLog(**provenance, record=record))
            else:
                span = TraceSpanObservation.model_validate(row["span"])
                if span.service != row["service"] or span.start_at != event_at:
                    raise FrozenSourceIntegrityError(
                        "Trace payload disagrees with its stored service/event index"
                    )
                if span.end_at is not None and (
                    span.end_at.utcoffset() is None or span.end_at < span.start_at
                ):
                    raise FrozenSourceIntegrityError("Trace end is unzoned or precedes its start")
                if span.end_at is not None and captured < span.end_at:
                    raise FrozenSourceIntegrityError("Trace capture precedes its recorded end")
                spans.append(FrozenSpan(**provenance, record=span))
    evidence = FrozenRequestEvidence(
        incident_id=str(boundary_incident),
        run_id=run_id,
        claim_id=claim_id,
        episode_start=episode_start,
        episode_end=episode_end,
        frozen_cutoff=window_end,
        caller_service=caller_service,
        configured_peer=configured_peer,
        logs=tuple(logs),
        spans=tuple(spans),
    )
    return ManifestRequestEvidence(
        evidence,
        digest,
        has_attributes,
        diagnoses[0].engine_version,
        diagnoses[0].revision_number,
        diagnoses[0].recorded_resolution,
    )
