"""Typed contracts shared by fake and live model providers."""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

ProviderFailureCategory = Literal[
    "BAD_REQUEST",
    "AUTHENTICATION",
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "RATE_LIMIT",
    "TIMEOUT",
    "CONNECTION",
    "SERVER_ERROR",
    "API_STATUS_ERROR",
    "UNKNOWN",
]


class ProviderErrorCode(StrEnum):
    """Stable provider failure categories exposed to the runtime."""

    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    BAD_REQUEST = "BAD_REQUEST"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    UNPROCESSABLE_REQUEST = "UNPROCESSABLE_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RESPONSE_FAILED = "RESPONSE_FAILED"
    RESPONSE_INCOMPLETE = "RESPONSE_INCOMPLETE"
    RESPONSE_ERROR = "RESPONSE_ERROR"
    OUTPUT_MESSAGE_MISSING = "OUTPUT_MESSAGE_MISSING"
    MULTIPLE_OUTPUT_MESSAGES = "MULTIPLE_OUTPUT_MESSAGES"
    OUTPUT_REFUSAL = "OUTPUT_REFUSAL"
    OUTPUT_TEXT_MISSING = "OUTPUT_TEXT_MISSING"
    MULTIPLE_OUTPUT_TEXT_ITEMS = "MULTIPLE_OUTPUT_TEXT_ITEMS"
    MULTIPLE_OUTPUT_TEXT_PAYLOADS = "MULTIPLE_OUTPUT_TEXT_PAYLOADS"
    DECISION_FUNCTION_MISSING = "DECISION_FUNCTION_MISSING"
    MULTIPLE_DECISION_FUNCTION_CALLS = "MULTIPLE_DECISION_FUNCTION_CALLS"
    UNEXPECTED_FUNCTION_CALL = "UNEXPECTED_FUNCTION_CALL"
    FUNCTION_ARGUMENTS_INVALID_JSON = "FUNCTION_ARGUMENTS_INVALID_JSON"
    FUNCTION_ARGUMENTS_SCHEMA_INVALID = "FUNCTION_ARGUMENTS_SCHEMA_INVALID"
    INVALID_STOP_REASON = "INVALID_STOP_REASON"
    JSON_DECODE_FAILED = "JSON_DECODE_FAILED"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    CONTEXT_LIMIT_EXCEEDED = "CONTEXT_LIMIT_EXCEEDED"
    LIVE_MODEL_DISABLED = "LIVE_MODEL_DISABLED"
    LIVE_MODEL_BUDGET_LEDGER_REQUIRED = "LIVE_MODEL_BUDGET_LEDGER_REQUIRED"
    LIVE_MODEL_BUDGET_LEDGER_MISMATCH = "LIVE_MODEL_BUDGET_LEDGER_MISMATCH"
    LIVE_MODEL_BUDGET_EXHAUSTED = "LIVE_MODEL_BUDGET_EXHAUSTED"
    BENCHMARK_MODEL_POLICY_VIOLATION = "BENCHMARK_MODEL_POLICY_VIOLATION"


class ModelMessage(BaseModel):
    """One compact message sent to a model provider."""

    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ToolSchemaDescriptor(BaseModel):
    """Safe provider-facing schema derived from one registered tool contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


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
    allowed_decisions: (
        tuple[Literal["CALL_TOOLS", "SUBMIT_HYPOTHESIS", "SUBMIT_DIAGNOSIS", "STOP"], ...] | None
    ) = None
    allowed_v5_actions: tuple[str, ...] | None = None
    allowed_v5_operations: tuple[str, ...] | None = None
    allowed_v5_targets: tuple[str, ...] | None = None
    allowed_v5_action_capabilities: dict[str, Any] | None = None
    allowed_tool_names: tuple[str, ...] | None = None
    tool_schemas: tuple[ToolSchemaDescriptor, ...] | None = None


class ResponseEnvelopeMetadata(BaseModel):
    """Safe, bounded metadata describing a Responses API envelope."""

    model_config = ConfigDict(extra="forbid", strict=True)

    response_id: str | None = None
    response_status: str | None = None
    has_error: bool = False
    error_type: str | None = None
    error_code: str | None = None
    error_param: str | None = None
    incomplete_reason: str | None = None
    output_item_count: int = Field(ge=0, default=0)
    output_item_types: list[str] = Field(default_factory=list)
    message_count: int = Field(ge=0, default=0)
    content_item_count: int = Field(ge=0, default=0)
    content_item_types: list[str] = Field(default_factory=list)
    output_text_item_count: int = Field(ge=0, default=0)
    refusal_item_count: int = Field(ge=0, default=0)
    output_text_lengths: list[int] = Field(default_factory=list)
    output_text_hashes: list[str] = Field(default_factory=list)
    function_call_count: int = Field(ge=0, default=0)
    decision_function_call_count: int = Field(ge=0, default=0)
    function_call_names: list[str] = Field(default_factory=list)
    function_call_argument_lengths: list[int] = Field(default_factory=list)
    function_call_argument_hashes: list[str] = Field(default_factory=list)
    json_error_position: int | None = Field(default=None, ge=0)
    schema_error_path: str | None = None


class ProviderFailureMetadata(BaseModel):
    """Safe, bounded metadata for an SDK/API failure before a usable response."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["openai"] = "openai"
    exception_class: str | None = Field(default=None, max_length=128)
    category: ProviderFailureCategory
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    api_error_type: str | None = Field(default=None, max_length=128)
    api_error_code: str | None = Field(default=None, max_length=128)
    api_error_param: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    message_summary: str | None = Field(default=None, max_length=500)


class ProviderAccountingSnapshot(BaseModel):
    """Non-secret provider and outbound-attempt counters for one process."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider_invocations: int = Field(ge=0, default=0)
    outbound_api_attempts: int = Field(ge=0, default=0)
    provider_retries: int = Field(ge=0, default=0)
    shared_ledger_consumed: int = Field(ge=0, default=0)


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
    response_metadata: ResponseEnvelopeMetadata | None = None


class ModelProvider(Protocol):
    """Minimal provider interface consumed by the investigation runtime."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one structured model response."""


class ProviderError(RuntimeError):
    """Typed provider error without exposing credentials or raw secrets."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        metadata: ResponseEnvelopeMetadata | None = None,
        failure_metadata: ProviderFailureMetadata | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.metadata = metadata
        self.failure_metadata = failure_metadata


def messages_to_dicts(messages: Sequence[ModelMessage]) -> list[dict[str, str]]:
    """Convert validated messages to the SDK-neutral wire shape."""
    return [message.model_dump() for message in messages]
