"""SQLAlchemy persistence schema for the deterministic core."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Declarative metadata root."""


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
    """Normalized alert and optional incident association."""

    __tablename__ = "alerts"

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
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
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
    before: Mapped[dict[str, Any]] = mapped_column("before", JSON, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column("after", JSON, nullable=False)
    revision: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)


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
