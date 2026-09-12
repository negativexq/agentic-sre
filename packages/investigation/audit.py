"""Non-secret investigation usage and termination audit records."""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.investigation.contracts import (
    DecisionType,
    StopReason,
    TerminationReason,
    ValidationStage,
)


class InvestigationAuditRecord(BaseModel):
    """One run-level record suitable for benchmark reporting."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: UUID
    incident_id: UUID
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_requests_total: int = Field(ge=0, default=0)
    duplicate_requests_suppressed: int = Field(ge=0, default=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    estimated_api_calls: int = Field(ge=0)
    actual_api_calls: int = Field(ge=0)
    logical_model_turns: int = Field(ge=0, default=0)
    provider_invocations: int = Field(ge=0, default=0)
    outbound_api_attempts: int = Field(ge=0, default=0)
    provider_retries: int = Field(ge=0, default=0)
    shared_ledger_consumed: int = Field(ge=0, default=0)
    model_calls_limit: int = Field(ge=0)
    terminal_decision: DecisionType | None = None
    stop_reason: StopReason | None = None
    model_budget_exhausted_after_terminal_decision: bool = False
    validation_stage: ValidationStage | None = None
    validation_path: str | None = None
    validator: str | None = None
    termination_reason: TerminationReason
    recorded_at: datetime


class InvestigationTurnAudit(BaseModel):
    """Bounded, non-secret audit data for one provider/runtime turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: UUID
    incident_id: UUID
    turn_number: int = Field(gt=0, le=3)
    current_model_call: int = Field(gt=0, le=3)
    future_model_calls_after_decision: int = Field(ge=0, le=3)
    is_final_model_turn: bool
    allowed_decisions: list[str] = Field(default_factory=list, max_length=3)
    selected_decision_function: str | None = None
    requested_tool_count: int = Field(ge=0, le=8)
    requested_tool_names: list[str] = Field(default_factory=list, max_length=8)
    requested_tool_argument_keys: list[list[str]] = Field(default_factory=list, max_length=8)
    requested_tool_argument_types: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    requested_tool_argument_hashes: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    request_audits: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    duplicate_requests_suppressed: int = Field(ge=0, default=0)
    validation_stage: ValidationStage | None = None
    validation_result: str = "NOT_EVALUATED"
    error_code: str | None = None
    error_path: str | None = None
    tool_calls_attempted: int = Field(ge=0)
    tool_calls_succeeded: int = Field(ge=0)
    tool_calls_failed: int = Field(ge=0)
    evidence_ids_created: list[str] = Field(default_factory=list, max_length=12)
    model_input_tokens: int = Field(ge=0)
    model_output_tokens: int = Field(ge=0)
    recorded_at: datetime


class InvestigationAuditSink(Protocol):
    """Persistence boundary for run-level audit data."""

    def record(self, record: InvestigationAuditRecord) -> None:
        """Persist one non-secret record."""

    def record_turn(self, record: InvestigationTurnAudit) -> None:
        """Persist one bounded per-turn record."""


class InMemoryInvestigationAuditSink:
    """Deterministic audit sink used by offline tests."""

    def __init__(self) -> None:
        self.records: list[InvestigationAuditRecord] = []
        self.turn_records: list[InvestigationTurnAudit] = []

    def record(self, record: InvestigationAuditRecord) -> None:
        """Retain a validated record in memory."""
        self.records.append(record)

    def record_turn(self, record: InvestigationTurnAudit) -> None:
        """Retain a bounded per-turn record in memory."""
        self.turn_records.append(record)
