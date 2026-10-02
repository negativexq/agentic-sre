"""SQLAlchemy persistence schema for the deterministic core."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Declarative metadata root."""


def _now() -> datetime:
    return datetime.now(UTC)


# Provenance only: when a row was written. Knowledge membership comes from
# the evidence manifest, never from this column (M19 I4).
def _ingested_at() -> Mapped[datetime | None]:
    return mapped_column(UTCDateTime(), nullable=True, default=_now)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that round-trips consistently on every backend."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        """Normalize values to UTC before persistence."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        """Restore UTC tzinfo when a backend returns a naive datetime."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class IncidentRow(Base):
    """Current materialized incident state."""

    __tablename__ = "incidents"

    incident_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class IncidentEventRow(Base):
    """Immutable incident timeline event."""

    __tablename__ = "incident_events"
    __table_args__ = (
        UniqueConstraint("incident_id", "sequence", name="uq_incident_event_sequence"),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class AlertRow(Base):
    """One normalized alert occurrence attached to one incident episode.

    ``fingerprint`` identifies the recurring alert shape, not a lifetime
    incident. Multiple rows with the same fingerprint are therefore valid
    after an alert resolves and fires again.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "fingerprint", "starts_at", name="uq_alert_occurrence_fingerprint_starts_at"
        ),
    )

    alert_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="SET NULL"), nullable=True
    )
    alert_name: Mapped[str] = mapped_column(String(255), nullable=False)
    service: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    cluster: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    annotations: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class ChangeRecordRow(Base):
    """Immutable observed resource-change fact used by read-only investigation."""

    __tablename__ = "change_records"

    change_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(255), nullable=False)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="DEPLOYMENT")
    before: Mapped[dict[str, Any]] = mapped_column("before", JSON, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column("after", JSON, nullable=False)
    revision: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    ingested_at: Mapped[datetime | None] = _ingested_at()


class EventVersionRow(Base):
    """One observed Kubernetes event, kept so a frozen incident window can be
    re-read later without depending on the cluster's own event TTL (events are
    garbage-collected by Kubernetes after about an hour).
    """

    __tablename__ = "event_versions"
    __table_args__ = (
        UniqueConstraint("namespace", "dedup_key"),
        Index(
            "ix_event_versions_namespace_involved_kind_name_involved_uid",
            "namespace",
            "involved_kind",
            "involved_name",
            "involved_uid",
        ),
    )

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # involvedObject kind/name, for cheap filtering without a JSON query.
    involved_kind: Mapped[str] = mapped_column(String(255), nullable=False)
    involved_name: Mapped[str] = mapped_column(String(255), nullable=False)
    involved_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # (uid or name)|count|lastTimestamp: identifies one observed state of one
    # Kubernetes event object; a coalesced repeat (count/lastTimestamp advance)
    # gets a new key and is stored again, matching the object journal's model
    # of "one row per observed version".
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # first_at (or last_at, or observed_at) -- used to filter by incident window.
    event_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ingested_at: Mapped[datetime | None] = _ingested_at()
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class JournalArrivalRow(Base):
    """When the Connector observed a journaled object version or Event (connector contract §15).

    Kept apart from the evidence rows; evidence windows admit by it (late-evidence-design.md §3).
    """

    __tablename__ = "journal_arrivals"
    __table_args__ = (Index("ix_journal_arrivals_row", "journal", "version_id", unique=True),)

    arrival_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal: Mapped[str] = mapped_column(String(16), nullable=False)  # "event" or "object"
    version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ChangeStreamGapRow(Base):
    """A loss of continuity on the Connector's change stream (late-evidence-design.md §4.2).

    ``namespace``/``kind`` name the one scope that lost it (connector contract §15.4); both absent for a
    global gap. ``since`` is the last instant the scope(s) were observed continuously, absent when the
    Connector did not know it: such a gap may reach back to any time before ``at``.
    """

    __tablename__ = "change_stream_gaps"
    __table_args__ = (Index("ix_change_stream_gaps_at", "at"),)

    gap_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    since: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class StreamFollowRow(Base):
    """One control-plane process following one Connector run's change stream (late-evidence §4.2).

    ``followed_since`` is the Connector time from which the stream has been read without a break; a
    process that provably resumes where an earlier one stopped (same epoch, first sequence number read
    at most one past the earlier one's last) keeps the earlier instant.
    """

    __tablename__ = "stream_follow_segments"
    __table_args__ = (Index("ix_stream_follow_epoch", "epoch", "last_seq"),)

    segment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epoch: Mapped[str] = mapped_column(String(128), nullable=False)
    first_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    followed_since: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ObjectVersionRow(Base):
    """One observed version of a Kubernetes object: a content change or a lifecycle event.

    Consecutive duplicates are skipped; repeated content (A -> B -> A) is kept.
    """

    __tablename__ = "object_versions"
    __table_args__ = (
        Index(
            "ix_object_versions_namespace_kind_name_uid",
            "namespace",
            "kind",
            "name",
            "uid",
        ),
    )

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    # When the source says this version came to be; unknown (NULL) unless
    # the source states it. Never filled from observed_at.
    source_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ingested_at: Mapped[datetime | None] = _ingested_at()
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    lifecycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UPDATED", server_default="UPDATED"
    )


LIFECYCLE_OBSERVATION_TYPES = (
    "OBSERVED",
    "READY_TRUE",
    "READY_FALSE",
    "CONTAINER_STARTED",
    "CONTAINER_TERMINATED",
    "OOM_KILLED",
    "EVICTED",
    "DELETION_REQUESTED",
    "DELETED",
    "STATUS_SNAPSHOT",
)


class EntityInstanceRow(Base):
    """Materialized index of exact Kubernetes object instances, keyed by UID.

    Maintained by the collector and updated in place; it is an index, not
    evidence. RCA and replay must not read its time fields as temporal facts.
    """

    __tablename__ = "entity_instances"
    __table_args__ = (
        UniqueConstraint("namespace", "kind", "uid", name="uq_entity_instance_uid"),
        Index("ix_entity_instances_namespace_kind_name", "namespace", "kind", "name"),
    )

    instance_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_kind: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # metadata.creationTimestamp as reported by the API server.
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deleted_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class LifecycleObservationRow(Base):
    """One append-only lifecycle fact about one exact instance (authoritative evidence).

    ``source_at`` is when the source says it happened (e.g. a condition's
    lastTransitionTime) and may be unknown; ``observed_at`` is when the
    collector saw it; ``ingested_at`` is when the row was written.
    """

    __tablename__ = "lifecycle_observations"
    __table_args__ = (
        UniqueConstraint(
            "instance_uid", "type", "observed_at", "source", name="uq_lifecycle_observation_fact"
        ),
        CheckConstraint(
            "type IN (" + ", ".join(f"'{item}'" for item in LIFECYCLE_OBSERVATION_TYPES) + ")",
            name="ck_lifecycle_observation_type",
        ),
        Index(
            "ix_lifecycle_observations_instance",
            "namespace",
            "kind",
            "instance_uid",
        ),
        Index("ix_lifecycle_observations_namespace_observed", "namespace", "observed_at"),
    )

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # lifecycle:<namespace>:<kind>:<uid>:<seq>
    evidence_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    instance_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def _one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IN (" + ", ".join(f"'{item}'" for item in values) + ")", name=name
    )


# Why a diagnosis revision was produced; LEGACY marks rows stored before revisions.
DIAGNOSIS_TRIGGERS = (
    "INITIAL",
    "MANUAL",
    "EVIDENCE_DEADLINE",
    "LEGACY",
    "ALERT_REFIRED",
    "RESOLVED",
)


class DiagnosisRow(Base):
    """A stored diagnosis for an incident; the latest one is shown."""

    __tablename__ = "diagnoses"
    __table_args__ = (
        UniqueConstraint("incident_id", "revision_number", name="uq_diagnosis_incident_revision"),
        _one_of('"trigger"', DIAGNOSIS_TRIGGERS, "ck_diagnosis_trigger"),
    )

    diagnosis_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    root_cause: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    # The diagnosis pipeline run that produced this row, binding it to its
    # timeline events. Nullable for diagnoses stored before this existed.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Revision metadata (0022; number and trigger required since 0023). Legacy
    # rows carry a backfilled number and trigger LEGACY; the other provenance
    # fields have no legacy source and stay NULL there.
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_diagnosis_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("diagnoses.diagnosis_id", name="fk_diagnoses_previous_diagnosis_id"),
        nullable=True,
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    window_end: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tape_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    epistemic_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


# Rule evidence a diagnosis revision could not yet decide (M19-5.4).
EVIDENCE_REQUIREMENT_KINDS = ("STATUS_CONTINUITY", "RESOURCE_COVERAGE")
EVIDENCE_REQUIREMENT_STATUSES = (
    "OPEN",
    "SATISFIED_BY_REVISION",
    "SUPERSEDED_BY_REVISION",
    "EXPIRED",
)
_OPEN_REQUIREMENT = text("status = 'OPEN'")


class EvidenceRequirementRow(Base):
    """A pending rule requirement opened by one diagnosis revision.

    Scheduler state, not authoritative evidence (M19 §1.5): ``status`` moves
    through its lifecycle in place. At most one row per ``requirement_key`` is
    OPEN; closed rows for the same key are kept as history. The canonical
    formula of ``requirement_key`` and the meaning of ``targets`` are defined
    by the requirement writer (M19-5.5), not by this schema.
    """

    __tablename__ = "evidence_requirements"
    __table_args__ = (
        _one_of("kind", EVIDENCE_REQUIREMENT_KINDS, "ck_evidence_requirement_kind"),
        _one_of("status", EVIDENCE_REQUIREMENT_STATUSES, "ck_evidence_requirement_status"),
        CheckConstraint(
            "json_typeof(targets) = 'array'", name="ck_evidence_requirement_targets_array"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "json_type(targets) = 'array'", name="ck_evidence_requirement_targets_array"
        ).ddl_if(dialect="sqlite"),
        Index(
            "uq_evidence_requirements_open_key",
            "requirement_key",
            unique=True,
            postgresql_where=_OPEN_REQUIREMENT,
            sqlite_where=_OPEN_REQUIREMENT,
        ),
    )

    requirement_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requirement_key: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    diagnosis_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("diagnoses.diagnosis_id", name="fk_evidence_requirements_diagnosis_id"),
        nullable=False,
    )
    hypothesis_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    targets: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class InvestigationRunRow(Base):
    """Versioned, append-only audit artifact for one bounded diagnosis run."""

    __tablename__ = "investigation_runs"

    diagnosis_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ReportRow(Base):
    """An immutable incident report snapshot pinned to one diagnosis run."""

    __tablename__ = "report_snapshots"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    diagnosis_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class EmailDeliveryRow(Base):
    """Audit record of a report share over email (recipients, status, errors)."""

    __tablename__ = "email_deliveries"
    # Idempotency is scoped to the report: the same client key may be reused for a
    # different report without colliding, and a replay is matched by (report, key).
    __table_args__ = (
        UniqueConstraint("report_id", "idempotency_key", name="uq_email_delivery_report_key"),
    )

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    incident_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    recipients: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LogObservationRow(Base):
    """One bounded, normalized log observation captured for replay."""

    __tablename__ = "log_observations"
    __table_args__ = (
        UniqueConstraint("incident_id", "dedup_key", name="uq_log_observation_incident_key"),
    )

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False, index=True
    )
    service: Mapped[str] = mapped_column(String(255), nullable=False)
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    ingested_at: Mapped[datetime | None] = _ingested_at()
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String(4000), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(512), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(255), nullable=False, default="loki")
    # The provider read that captured this observation (M19-3.9 fills it).
    source_read_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("investigation_reads.read_id", name="fk_log_observations_source_read_id"),
        nullable=True,
    )


CALLER_CLASSES = ("CAPTURE", "ENGINE", "INVESTIGATION")
READ_STATUSES = ("SUCCESS", "ERROR")


class SnapshotCycleRow(Base):
    """One diagnosis capture's cluster listing: when it ran and which scopes completed."""

    __tablename__ = "snapshot_cycles"

    cycle_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_scopes: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    failed_scopes: Mapped[list[Any]] = mapped_column(JSON, nullable=False)


class SnapshotCycleObjectRow(Base):
    """One object exactly as a snapshot cycle listed it, full body including status."""

    __tablename__ = "snapshot_cycle_objects"

    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("snapshot_cycles.cycle_id"), primary_key=True
    )
    object_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # snapshot:<cycle_id>:<object_key>
    evidence_id: Mapped[str] = mapped_column(String(600), nullable=False, unique=True)


class RunEvidenceManifestRow(Base):
    """One exact source id a diagnosis run was allowed to know, in manifest order."""

    __tablename__ = "run_evidence_manifest"
    __table_args__ = (
        UniqueConstraint("run_id", "source_type", "source_id", name="uq_manifest_run_source"),
        UniqueConstraint("run_id", "sequence", name="uq_manifest_run_sequence"),
    )

    manifest_entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(600), nullable=False)
    # Content frozen at manifest time for sources whose rows can still change
    # (alerts are updated on resolve); NULL for append-only sources.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class InvestigationReadRow(Base):
    """One provider read of a run, in call order: the replay tape.

    ``query_key`` is not unique: the same canonical query may be read several
    times in one run. ``(run_id, sequence)`` is the order replay follows.
    """

    __tablename__ = "investigation_reads"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_investigation_read_run_sequence"),
        _one_of("caller_class", CALLER_CLASSES, "ck_investigation_read_caller_class"),
        _one_of("status", READ_STATUSES, "ck_investigation_read_status"),
    )

    read_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    caller_class: Mapped[str] = mapped_column(String(16), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    query_key: Mapped[str] = mapped_column(Text(), nullable=False)
    query_descriptor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    observation: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)


class EvidenceRow(Base):
    """Provenance-backed normalized evidence."""

    __tablename__ = "evidence"

    evidence_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_system: Mapped[str] = mapped_column(String(255), nullable=False)
    observation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    time_window: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tool_call_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    raw_result_reference: Mapped[str] = mapped_column(String(1000), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class HypothesisRow(Base):
    """Structured hypothesis record."""

    __tablename__ = "hypotheses"

    hypothesis_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    affected_component: Mapped[str] = mapped_column(String(255), nullable=False)
    mechanism: Mapped[str] = mapped_column(String(1000), nullable=False)
    suspected_trigger: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    counter_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class RemediationProposalRow(Base):
    """Typed remediation proposal record."""

    __tablename__ = "remediation_proposals"

    proposal_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(4000), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    reversible: Mapped[bool] = mapped_column(nullable=False)


class PolicyDecisionRow(Base):
    """Policy evaluation result."""

    __tablename__ = "policy_decisions"

    decision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(4000), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(255), nullable=False)


class ActionExecutionRow(Base):
    """Action execution audit record."""

    __tablename__ = "action_executions"

    execution_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class VerificationResultRow(Base):
    """Verification result with serialized check payload."""

    __tablename__ = "verification_results"

    result_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    summary: Mapped[str] = mapped_column(String(4000), nullable=False)


class ToolCallRow(Base):
    """Audited investigation tool invocation placeholder."""

    __tablename__ = "tool_calls"

    tool_call_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


ALERT_COVERAGE_STATUSES = ("OPEN", "CLOSED_FAILURE", "BROKEN_GAP")


class AlertCoverageSegmentRow(Base):
    """One contiguous span in which the alert channel was actively observed.

    ``OPEN`` is extended by each successful poll. A failed poll ends it at its last
    success (``CLOSED_FAILURE``); a later success after a polling gap ends it there
    too (``BROKEN_GAP``). Alert state outside a segment is unknown.
    """

    __tablename__ = "alert_coverage_segments"
    __table_args__ = (
        _one_of("status", ALERT_COVERAGE_STATUSES, "ck_alert_coverage_segment_status"),
        Index("ix_alert_coverage_segments_source_status", "source", "status"),
    )

    segment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_success_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class AlertCoveragePollRow(Base):
    """Audit of one alert-channel poll, successful or not."""

    __tablename__ = "alert_coverage_polls"

    poll_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    segment_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("alert_coverage_segments.segment_id", name="fk_alert_coverage_poll_segment"),
        nullable=True,
    )
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_alerts: Mapped[int | None] = mapped_column(Integer, nullable=True)
