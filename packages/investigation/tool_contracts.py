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

    def project(value: Any) -> dict[str, Any]:
        """Project one Pydantic field to the safe descriptor vocabulary."""
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        if "anyOf" in value and isinstance(value["anyOf"], list):
            types: list[str] = []
            branches: list[dict[str, Any]] = []
            for branch in value["anyOf"]:
                projected = project(branch)
                branch_type = projected.get("type")
                if isinstance(branch_type, list):
                    types.extend(item for item in branch_type if item not in types)
                elif isinstance(branch_type, str) and branch_type not in types:
                    types.append(branch_type)
                branches.append(projected)
            if types:
                result["type"] = types
            for branch in branches:
                for key in ("enum", "minLength", "maxLength", "minimum", "maximum"):
                    if key in branch and key not in result:
                        result[key] = branch[key]
            return result
        field_type = value.get("type")
        if isinstance(field_type, str):
            result["type"] = field_type
        if isinstance(value.get("enum"), list):
            result["enum"] = list(value["enum"])
        for key in ("minLength", "maxLength", "minimum", "maximum"):
            if key in value:
                result[key] = value[key]
        return result

    result: dict[str, Any] = {}
    for name, value in properties.items():
        if not isinstance(value, dict):
            continue
        item = project(value)
        item["required"] = name in required
        result[name] = item
    return result
