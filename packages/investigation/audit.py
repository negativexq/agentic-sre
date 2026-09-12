"""Non-secret investigation usage and termination audit records."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.investigation.contracts import DecisionType, StopReason, TerminationReason


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
    termination_reason: TerminationReason
    recorded_at: datetime


class InvestigationAuditSink(Protocol):
    """Persistence boundary for run-level audit data."""

    def record(self, record: InvestigationAuditRecord) -> None:
        """Persist one non-secret record."""


class InMemoryInvestigationAuditSink:
    """Deterministic audit sink used by offline tests."""

    def __init__(self) -> None:
        self.records: list[InvestigationAuditRecord] = []

    def record(self, record: InvestigationAuditRecord) -> None:
        """Retain a validated record in memory."""
        self.records.append(record)
