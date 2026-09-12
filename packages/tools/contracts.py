"""Typed contracts for bounded read-only investigation tools."""

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from packages.contracts import TimeWindow


class ToolErrorCode(StrEnum):
    """Stable investigation tool failure codes."""

    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    INVALID_QUERY = "INVALID_QUERY"
    RESULT_LIMIT_EXCEEDED = "RESULT_LIMIT_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"


class ToolRequest(BaseModel):
    """Common bounded invocation envelope."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    incident_id: UUID
    tool_call_id: UUID = Field(default_factory=uuid4)
    timeout_ms: int = Field(gt=0, le=10_000)
    max_results: int = Field(gt=0, le=10_000)
    max_bytes: int = Field(gt=0, le=10_000_000)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    """Successful typed tool result."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_call_id: UUID
    data: dict[str, Any]
    result_count: int = Field(ge=0)
    effective_time_window: TimeWindow | None = None


class ToolFailure(BaseModel):
    """Typed, auditable tool failure."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_call_id: UUID
    code: ToolErrorCode
    message: str = Field(min_length=1)


ToolResult = ToolResponse | ToolFailure
