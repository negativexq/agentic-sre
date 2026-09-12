"""Explicit OpenAI Responses API boundary with local credit guards."""

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Protocol, cast

from packages.provider.budget import LiveModelBudget
from packages.provider.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
    ResponseEnvelopeMetadata,
    messages_to_dicts,
)


@dataclass(frozen=True, slots=True)
class LiveModelConfig:
    """Environment-derived live model settings."""

    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "none"
    enabled: bool = False


def live_model_config() -> LiveModelConfig:
    """Read non-secret model settings from the environment."""
    return LiveModelConfig(
        model=os.getenv("SRE_MODEL", "gpt-5.6-luna"),
        reasoning_effort=os.getenv("SRE_REASONING_EFFORT", "none"),
        enabled=os.getenv("SRE_LIVE_MODEL_ENABLED", "false").lower() == "true",
    )


class ResponsesTransport(Protocol):
    """Small seam allowing the SDK transport to be tested without the network."""

    def create(self, **kwargs: Any) -> Any:
        """Create one Responses API request."""


_UNSUPPORTED_STRICT_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "default",
    }
)


def _compile_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Compile Pydantic JSON Schema to the strict Responses API subset.

    Pydantic constraints remain enforced when the response is parsed locally.
    The wire schema must instead use the provider's strict subset: every object
    property is required, every object rejects unknown keys, and unsupported
    validation-only keywords are omitted.
    """

    def compile_node(node: Any) -> Any:
        if isinstance(node, list):
            return [compile_node(item) for item in node]
        if not isinstance(node, dict):
            return node

        compiled = {
            key: compile_node(value)
            for key, value in node.items()
            if key not in _UNSUPPORTED_STRICT_KEYWORDS
        }
        if compiled.get("type") == "object":
            properties = compiled.get("properties")
            if isinstance(properties, dict):
                compiled["required"] = list(properties)
            else:
                compiled["properties"] = {}
                compiled["required"] = []
            compiled["additionalProperties"] = False
        return compiled

    result = compile_node(schema)
    if not isinstance(result, dict):
        raise ValueError("response schema root must be an object")
    return result


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK object or a JSON-like fixture."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_string(value: Any, *, limit: int = 128) -> str | None:
    """Return a bounded diagnostic scalar without retaining arbitrary payloads."""
    return value[:limit] if isinstance(value, str) else None


def _safe_strings(values: list[Any]) -> list[str]:
    """Return bounded diagnostic strings with None values removed."""
    result: list[str] = []
    for value in values:
        safe = _safe_string(value)
        if safe is not None:
            result.append(safe)
    return result


def _response_metadata(raw: Any) -> ResponseEnvelopeMetadata:
    """Collect only bounded envelope and output-shape metadata."""
    output = _field(raw, "output")
    items = output if isinstance(output, list) else []
    item_types = _safe_strings([_field(item, "type") for item in items[:32]])
    messages = [item for item in items if _field(item, "type") == "message"]
    content_items: list[Any] = []
    for message in messages:
        content = _field(message, "content")
        if isinstance(content, list):
            content_items.extend(content[:32])
    content_types = _safe_strings([_field(item, "type") for item in content_items[:64]])
    output_texts = [
        _field(item, "text")
        for item in content_items
        if _field(item, "type") == "output_text" and isinstance(_field(item, "text"), str)
    ]
    refusals = [item for item in content_items if _field(item, "type") == "refusal"]
    error = _field(raw, "error")
    incomplete = _field(raw, "incomplete_details")
    return ResponseEnvelopeMetadata(
        response_id=_safe_string(_field(raw, "id") or _field(raw, "response_id")),
        response_status=_safe_string(_field(raw, "status")),
        has_error=error is not None,
        error_type=_safe_string(_field(error, "type")),
        error_code=_safe_string(_field(error, "code")),
        error_param=_safe_string(_field(error, "param")),
        incomplete_reason=_safe_string(_field(incomplete, "reason")),
        output_item_count=len(items),
        output_item_types=item_types,
        message_count=len(messages),
        content_item_count=len(content_items),
        content_item_types=content_types,
        output_text_item_count=len(output_texts),
        refusal_item_count=len(refusals),
        output_text_lengths=[len(item) for item in output_texts[:32]],
        output_text_hashes=[sha256(item.encode("utf-8")).hexdigest() for item in output_texts[:32]],
    )


def _with_output_metadata(
    metadata: ResponseEnvelopeMetadata,
    output_text: str,
) -> ResponseEnvelopeMetadata:
    """Add safe diagnostics for a legacy or direct output text field."""
    return metadata.model_copy(
        update={
            "output_text_item_count": 1,
            "output_text_lengths": [len(output_text)],
            "output_text_hashes": [sha256(output_text.encode("utf-8")).hexdigest()],
        }
    )


def _extract_structured_text(raw: Any) -> tuple[str, ResponseEnvelopeMetadata]:
    """Resolve exactly one assistant structured output segment, fail-closed."""
    metadata = _response_metadata(raw)
    status = metadata.response_status
    if status is not None and status != "completed":
        code = (
            ProviderErrorCode.RESPONSE_ERROR
            if metadata.has_error
            else ProviderErrorCode.RESPONSE_INCOMPLETE
            if metadata.incomplete_reason or status != "failed"
            else ProviderErrorCode.RESPONSE_FAILED
        )
        raise ProviderError(code, "provider response was not completed", metadata=metadata)
    if metadata.has_error:
        raise ProviderError(
            ProviderErrorCode.RESPONSE_ERROR,
            "provider response contained an error",
            metadata=metadata,
        )
    if metadata.incomplete_reason is not None:
        raise ProviderError(
            ProviderErrorCode.RESPONSE_INCOMPLETE,
            "provider response was incomplete",
            metadata=metadata,
        )

    output = _field(raw, "output")
    if output is None:
        output_text = _field(raw, "output_text")
        if isinstance(output_text, str):
            return output_text, _with_output_metadata(metadata, output_text)
    if not isinstance(output, list) or metadata.output_item_count == 0:
        raise ProviderError(
            ProviderErrorCode.OUTPUT_MESSAGE_MISSING,
            "provider response contained no output message",
            metadata=metadata,
        )
    if metadata.message_count == 0:
        raise ProviderError(
            ProviderErrorCode.OUTPUT_MESSAGE_MISSING,
            "provider response contained no assistant message",
            metadata=metadata,
        )
    if metadata.message_count > 1:
        raise ProviderError(
            ProviderErrorCode.MULTIPLE_OUTPUT_MESSAGES,
            "provider response contained multiple assistant messages",
            metadata=metadata,
        )
    if metadata.refusal_item_count > 0:
        raise ProviderError(
            ProviderErrorCode.OUTPUT_REFUSAL,
            "provider response contained a refusal",
            metadata=metadata,
        )
    if metadata.output_text_item_count == 0:
        raise ProviderError(
            ProviderErrorCode.OUTPUT_TEXT_MISSING,
            "assistant message contained no output text",
            metadata=metadata,
        )
    if metadata.output_text_item_count > 1:
        raise ProviderError(
            ProviderErrorCode.MULTIPLE_OUTPUT_TEXT_ITEMS,
            "assistant message contained multiple output text items",
            metadata=metadata,
        )
    for item in output:
        if _field(item, "type") != "message":
            continue
        content = _field(item, "content")
        if isinstance(content, list):
            for part in content:
                if _field(part, "type") == "output_text" and isinstance(_field(part, "text"), str):
                    return _field(part, "text"), metadata
    raise ProviderError(
        ProviderErrorCode.OUTPUT_TEXT_MISSING,
        "assistant output text payload was not readable",
        metadata=metadata,
    )


def _json_schema_error(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
    root_schema: dict[str, Any] | None = None,
) -> str | None:
    """Validate the response shape without applying provider-unsupported limits."""
    root_schema = root_schema or schema
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            return path
        definition_name = reference.removeprefix("#/$defs/")
        definitions = root_schema.get("$defs", {})
        definition = definitions.get(definition_name) if isinstance(definitions, dict) else None
        if not isinstance(definition, dict):
            return path
        return _json_schema_error(value, definition, path=path, root_schema=root_schema)
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not isinstance(branches, list):
            return path
        if any(
            _json_schema_error(value, branch, path=path, root_schema=root_schema) is None
            for branch in branches
        ):
            return None
        return path
    if "enum" in schema and value not in schema["enum"]:
        return path

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if any(
            _json_schema_error(value, {"type": item}, path=path, root_schema=root_schema) is None
            for item in schema_type
        ):
            return None
        return path
    if schema_type == "null":
        return None if value is None else path
    if schema_type == "string":
        return None if isinstance(value, str) else path
    if schema_type == "boolean":
        return None if isinstance(value, bool) else path
    if schema_type == "integer":
        return None if isinstance(value, int) and not isinstance(value, bool) else path
    if schema_type == "number":
        return None if isinstance(value, int | float) and not isinstance(value, bool) else path
    if schema_type == "array":
        if not isinstance(value, list):
            return path
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error_path = _json_schema_error(
                    item, item_schema, path=f"{path}[{index}]", root_schema=root_schema
                )
                if error_path is not None:
                    return error_path
        return None
    if schema_type == "object":
        if not isinstance(value, dict):
            return path
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return path
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    return f"{path}.{key}"
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                return f"{path}.{sorted(unexpected)[0]}"
        for key, property_schema in properties.items():
            if key in value and isinstance(property_schema, dict):
                error_path = _json_schema_error(
                    value[key], property_schema, path=f"{path}.{key}", root_schema=root_schema
                )
                if error_path is not None:
                    return error_path
        return None
    return None


class OpenAIProvider:
    """Call OpenAI only when explicitly enabled and budget-authorized."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        budget: LiveModelBudget,
        config: LiveModelConfig | None = None,
        transport: ResponsesTransport | None = None,
        max_retry: int = 1,
    ) -> None:
        if max_retry not in (0, 1):
            raise ValueError("max_retry must be 0 or 1")
        self._budget = budget
        self._config = config or live_model_config()
        self._transport = transport or self._build_transport()
        self._max_retry = max_retry

    def _build_transport(self) -> ResponsesTransport:
        """Construct the SDK client lazily, keeping imports out of offline paths."""
        if not self._config.enabled:
            raise ProviderError(
                ProviderErrorCode.LIVE_MODEL_DISABLED,
                "live model execution is disabled",
            )
        if not os.getenv("OPENAI_API_KEY"):
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "OPENAI_API_KEY is required for live model execution",
            )
        try:
            from openai import OpenAI
        except ImportError as error:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "OpenAI SDK is not installed",
            ) from error
        return cast(ResponsesTransport, OpenAI().responses)

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Make one or at most one explicitly configured transient retry."""
        if request.model != self._config.model:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE, "request model is not configured model"
            )
        if request.reasoning_effort != self._config.reasoning_effort:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "request reasoning effort is not configured reasoning effort",
            )
        if not self._config.enabled:
            raise ProviderError(
                ProviderErrorCode.LIVE_MODEL_DISABLED,
                "live model execution is disabled",
            )

        attempts = self._max_retry + 1
        for attempt in range(attempts):
            self._budget.consume()
            started = monotonic()
            try:
                raw = self._transport.create(
                    model=request.model,
                    input=messages_to_dicts(request.messages),
                    reasoning={"effort": request.reasoning_effort},
                    max_output_tokens=request.max_output_tokens,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": request.response_schema_name,
                            "schema": _compile_strict_schema(request.response_schema),
                            "strict": True,
                        }
                    },
                    timeout=request.timeout_ms / 1000,
                )
                return self._normalize_response(request, raw, started)
            except ProviderError:
                raise
            except (TimeoutError, ConnectionError) as error:
                if attempt + 1 < attempts:
                    continue
                if attempt + 1 == attempts:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT
                        if isinstance(error, TimeoutError)
                        else ProviderErrorCode.PROVIDER_UNAVAILABLE,
                        "live provider request failed",
                    ) from error
            except Exception as error:
                status_code = getattr(error, "status_code", None)
                error_name = error.__class__.__name__.lower()
                if "timeout" in error_name:
                    if attempt + 1 < attempts:
                        continue
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT,
                        "live provider request timed out",
                    ) from error
                retryable = status_code == 429 or (
                    isinstance(status_code, int) and status_code >= 500
                )
                if retryable and attempt + 1 < attempts:
                    continue
                message = str(error).lower()
                if "context" in message and "limit" in message:
                    code = ProviderErrorCode.CONTEXT_LIMIT_EXCEEDED
                elif status_code == 429:
                    code = ProviderErrorCode.RATE_LIMITED
                else:
                    code = ProviderErrorCode.PROVIDER_UNAVAILABLE
                raise ProviderError(code, "live provider request failed") from error
        raise AssertionError("provider retry loop must return or raise")

    def _normalize_response(
        self,
        request: ModelRequest,
        raw: Any,
        started: float,
    ) -> ModelResponse:
        """Parse structured JSON and usage without retaining raw provider output."""
        output_text, metadata = _extract_structured_text(raw)
        try:
            structured_output = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise ProviderError(
                ProviderErrorCode.JSON_DECODE_FAILED,
                "provider structured output was not valid JSON",
                metadata=metadata.model_copy(update={"json_error_position": error.pos}),
            ) from error
        if not isinstance(structured_output, dict):
            raise ProviderError(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "provider structured output was not an object",
                metadata=metadata,
            )
        schema_error_path = _json_schema_error(structured_output, request.response_schema)
        if schema_error_path is not None:
            raise ProviderError(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "provider structured output did not match response schema",
                metadata=metadata.model_copy(update={"schema_error_path": schema_error_path}),
            )
        usage = getattr(raw, "usage", None)
        return ModelResponse(
            request_id=request.request_id,
            structured_output=structured_output,
            provider=self.provider_name,
            model=request.model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=int((monotonic() - started) * 1000),
            finish_reason=str(getattr(raw, "status", "completed")),
            response_metadata=metadata,
        )
