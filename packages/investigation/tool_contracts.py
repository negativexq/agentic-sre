"""Canonical argument contracts shared by tool descriptors and execution."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolArguments(BaseModel):
    """Strict base for model-requested tool arguments."""

    model_config = ConfigDict(extra="forbid", strict=True)


class AnyToolArguments(ToolArguments):
    """Compatibility contract for test-only custom tools."""

    model_config = ConfigDict(extra="allow", strict=True)


class ServiceArgs(ToolArguments):
    service: str = Field(min_length=1, max_length=80)


class ServiceWindowArgs(ServiceArgs):
    range_seconds: int | None = Field(default=None, ge=1, le=900)


class ServicePatternArgs(ServiceArgs):
    pattern: str | None = Field(default=None, min_length=1, max_length=100)


class ConsumerArgs(ToolArguments):
    consumer: str = Field(min_length=1, max_length=80)


class TraceIdArgs(ToolArguments):
    trace_id: str = Field(min_length=32, max_length=32)


class DeploymentArgs(ToolArguments):
    deployment: str = Field(min_length=1, max_length=80)


class ToolArgumentValidationError(ValueError):
    """Safe typed argument failure raised before backend execution."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.message = message


def descriptor_schema(model: type[ToolArguments]) -> dict[str, Any]:
    """Return one bounded, model-facing schema derived from the validator."""
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    result: dict[str, Any] = {}
    for name, value in properties.items():
        if not isinstance(value, dict):
            continue
        item = {key: item for key, item in value.items() if key in {"type", "enum"}}
        item["required"] = name in required
        result[name] = item
    return result
