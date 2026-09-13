"""HTTP error contracts for the control plane."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """Stable machine-readable API error."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    """Envelope used by all typed control-plane errors."""

    model_config = ConfigDict(extra="forbid", strict=True)

    error: ErrorDetail


class BenchmarkStatePreparationRequest(BaseModel):
    """Explicit authorization for local benchmark incident-state preparation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    environment: Literal["local-kind"]
    scope: Literal["incident-alert-state"]
    confirmation: Literal["reset-local-benchmark-state"]
    execution_id: str = Field(min_length=1, max_length=100)


class BenchmarkStatePreparationResponse(BaseModel):
    """Auditable result of the narrow benchmark-state preparation operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    environment: Literal["local-kind"]
    scope: Literal["incident-alert-state"]
    execution_id: str = Field(min_length=1, max_length=100)
    deleted_incidents: int = Field(ge=0)
    deleted_alerts: int = Field(ge=0)
    remaining_incidents: int = Field(ge=0)
    remaining_alerts: int = Field(ge=0)
