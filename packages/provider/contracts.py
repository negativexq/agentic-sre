"""Typed contracts shared by fake and live model providers."""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ProviderErrorCode(StrEnum):
    """Stable provider failure categories exposed to the runtime."""

    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    CONTEXT_LIMIT_EXCEEDED = "CONTEXT_LIMIT_EXCEEDED"
    LIVE_MODEL_DISABLED = "LIVE_MODEL_DISABLED"
    LIVE_MODEL_BUDGET_EXHAUSTED = "LIVE_MODEL_BUDGET_EXHAUSTED"


class ModelMessage(BaseModel):
    """One compact message sent to a model provider."""

    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ModelRequest(BaseModel):
    """Provider-independent structured-output request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    messages: list[ModelMessage] = Field(min_length=1, max_length=20)
    response_schema_name: str = Field(min_length=1, max_length=128)
    response_schema: dict[str, Any]
    model: str = Field(min_length=1)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"
    max_output_tokens: int = Field(gt=0, le=128_000)
    timeout_ms: int = Field(gt=0, le=120_000)


class ModelResponse(BaseModel):
    """Normalized structured provider response with usage metadata."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: UUID
    structured_output: dict[str, Any]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    finish_reason: str = Field(min_length=1)


class ModelProvider(Protocol):
    """Minimal provider interface consumed by the investigation runtime."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one structured model response."""


class ProviderError(RuntimeError):
    """Typed provider error without exposing credentials or raw secrets."""

    def __init__(self, code: ProviderErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def messages_to_dicts(messages: Sequence[ModelMessage]) -> list[dict[str, str]]:
    """Convert validated messages to the SDK-neutral wire shape."""
    return [message.model_dump() for message in messages]
