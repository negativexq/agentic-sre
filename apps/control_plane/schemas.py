"""HTTP error contracts for the control plane."""

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
