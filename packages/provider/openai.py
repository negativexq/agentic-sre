"""Explicit OpenAI Responses API boundary with local credit guards."""

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import Any, Protocol, cast

from packages.model_policy import ModelPolicyError, validate_agent_config
from packages.provider.budget import LiveModelBudget
from packages.provider.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderAccountingSnapshot,
    ProviderError,
    ProviderErrorCode,
    ResponseEnvelopeMetadata,
    ToolSchemaDescriptor,
    messages_to_dicts,
)

DECISION_FUNCTION_NAMES = (
    "request_investigation_tools",
    "submit_root_cause_hypothesis",
    "stop_investigation",
)
A1_DECISION_FUNCTION_NAMES = (
    "request_investigation_tools",
    "submit_causal_hypothesis",
    "stop_causal_investigation",
)
ITBENCH_DECISION_FUNCTION_NAMES = (
    "request_itbench_tools",
    "submit_itbench_diagnosis",
    "stop_itbench_investigation",
)
TERMINAL_DECISION_FUNCTION_NAMES = (
    "submit_root_cause_hypothesis",
    "stop_investigation",
)
DECISION_FUNCTION_DESCRIPTIONS = {
    "request_investigation_tools": (
        "Use only when additional bounded read-only evidence is necessary before a reliable "
        "terminal decision. Do not use when the current turn is terminal-only or no future "
        "model turn can interpret new evidence. Never request write actions."
    ),
    "submit_root_cause_hypothesis": (
        "Use when the supplied incident context and evidence identify the most likely affected "
        "component, mechanism, and trigger. Cite only evidence IDs supplied by the runtime. "
        "This is a terminal decision."
    ),
    "stop_investigation": (
        "Use to terminate without a root-cause hypothesis when evidence is insufficient, the "
        "investigation is complete without a justified conclusion, or no conclusion is warranted. "
        "This is a terminal decision."
    ),
}
A1_DECISION_FUNCTION_DESCRIPTIONS = {
    **DECISION_FUNCTION_DESCRIPTIONS,
    "submit_causal_hypothesis": (
        "Submit a structured causal hypothesis with symptom and causal workloads, an optional "
        "dependency resource, a controlled mechanism and runtime-owned evidence IDs."
    ),
    "stop_causal_investigation": (
        "Terminate without a causal hypothesis when the bounded runtime evidence is insufficient; "
        "report considered workloads/resources and missing evidence categories."
    ),
}
DECISION_TO_FUNCTION = {
    "CALL_TOOLS": "request_investigation_tools",
    "SUBMIT_HYPOTHESIS": "submit_root_cause_hypothesis",
    "STOP": "stop_investigation",
}
A1_DECISION_TO_FUNCTION = {
    "CALL_TOOLS": "request_investigation_tools",
    "SUBMIT_HYPOTHESIS": "submit_causal_hypothesis",
    "STOP": "stop_causal_investigation",
}
ITBENCH_DECISION_TO_FUNCTION = {
    "CALL_TOOLS": "request_itbench_tools",
    "SUBMIT_DIAGNOSIS": "submit_itbench_diagnosis",
    "STOP": "stop_itbench_investigation",
}
_STOP_REASON_VALUES = (
    "insufficient_evidence",
    "investigation_complete",
    "no_action_needed",
)


@dataclass(frozen=True, slots=True)
class LiveModelConfig:
    """Environment-derived live model settings."""

    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "none"
    enabled: bool = False


def live_model_config() -> LiveModelConfig:
    """Read explicitly supplied non-secret model settings from the environment."""
    return LiveModelConfig(
        model=os.getenv("SRE_MODEL", ""),
        reasoning_effort=os.getenv("SRE_REASONING_EFFORT", ""),
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

        compiled: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_STRICT_KEYWORDS:
                continue
            if key == "properties" and isinstance(value, dict):
                # Property names are data, not JSON Schema keywords. In
                # particular, a legitimate field named ``pattern`` must not
                # be removed while compiling the surrounding object.
                compiled[key] = {name: compile_node(item) for name, item in value.items()}
            else:
                compiled[key] = compile_node(value)
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


def _decision_function_schemas(
    schema: dict[str, Any],
    allowed_tool_names: tuple[str, ...] | None = None,
    tool_schemas: tuple[ToolSchemaDescriptor, ...] | None = None,
    *,
    a1_protocol: bool = False,
) -> dict[str, dict[str, Any]]:
    """Build strict transport schemas for the legacy or A1 decision protocol."""
    compiled = _compile_strict_schema(schema)
    definitions = compiled.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("investigation decision schema definitions are invalid")
    tool_request = definitions.get("ToolRequestSpec")
    hypothesis_name = "CausalHypothesis" if a1_protocol else "HypothesisSubmission"
    hypothesis = definitions.get(hypothesis_name)
    if not isinstance(tool_request, dict) or not isinstance(hypothesis, dict):
        raise ValueError("investigation decision schema definitions are incomplete")

    def object_schema(
        properties: dict[str, Any],
        required: list[str],
        used_definitions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if used_definitions:
            result["$defs"] = used_definitions
        return result

    reason = {"type": "string"}
    tool_request = dict(tool_request)
    if tool_schemas is not None:
        branches: list[dict[str, Any]] = []
        allowed = set(allowed_tool_names) if allowed_tool_names is not None else None
        for descriptor in tool_schemas:
            if allowed is not None and descriptor.name not in allowed:
                continue
            argument_properties: dict[str, Any] = {}
            argument_required: list[str] = []
            for key, descriptor_field in descriptor.arguments.items():
                if not isinstance(descriptor_field, dict):
                    continue
                projected = {
                    field_key: field_value
                    for field_key, field_value in descriptor_field.items()
                    if field_key
                    in {
                        "type",
                        "enum",
                        "minLength",
                        "maxLength",
                        "minimum",
                        "maximum",
                    }
                }
                field_type = projected.get("type", "string")
                if descriptor_field.get("required") is False:
                    if isinstance(field_type, list):
                        projected["type"] = (
                            [*field_type] if "null" in field_type else [*field_type, "null"]
                        )
                    else:
                        projected["type"] = [field_type, "null"]
                projected.pop("required", None)
                argument_properties[key] = projected
                argument_required.append(key)
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": [descriptor.name]},
                        "arguments": {
                            "type": "object",
                            "properties": argument_properties,
                            "required": argument_required,
                            "additionalProperties": False,
                        },
                    },
                    "required": ["tool", "arguments"],
                    "additionalProperties": False,
                }
            )
        tool_request_item: dict[str, Any] = {"anyOf": branches}
        tool_request_properties = dict(tool_request.get("properties", {}))
        tool_request_properties["tool"] = {"type": "string"}
        tool_request_properties["arguments"] = {"type": "object"}
        tool_request["properties"] = tool_request_properties
        tool_request["_provider_tool_request_item"] = tool_request_item
    elif allowed_tool_names is not None:
        properties = dict(tool_request.get("properties", {}))
        tool_property = dict(properties.get("tool", {"type": "string"}))
        tool_property["enum"] = list(allowed_tool_names)
        properties["tool"] = tool_property
        tool_request["properties"] = properties
    request_items = tool_request.pop(
        "_provider_tool_request_item", {"$ref": "#/$defs/ToolRequestSpec"}
    )
    request_definitions = {} if tool_schemas is not None else {"ToolRequestSpec": tool_request}
    if a1_protocol:
        structured_trigger = definitions.get("StructuredTrigger")
        stop = definitions.get("CausalStopDecision")
        if not isinstance(structured_trigger, dict) or not isinstance(stop, dict):
            raise ValueError("A1 causal decision schema definitions are incomplete")
        terminal_definitions = {
            name: value
            for name, value in definitions.items()
            if name
            in {
                "CausalStopDecision",
                "DependencyResourceId",
                "EvidenceCategory",
                "HypothesisMechanism",
                "StructuredTrigger",
                "StopReason",
                "TriggerType",
                "WorkloadComponentId",
            }
        }
        hypothesis_schema = {
            "reason": reason,
            "symptom_component": hypothesis["properties"]["symptom_component"],
            "causal_component": hypothesis["properties"]["causal_component"],
            "causal_resource": hypothesis["properties"]["causal_resource"],
            "mechanism": hypothesis["properties"]["mechanism"],
            "structured_trigger": hypothesis["properties"]["structured_trigger"],
            "causal_summary": hypothesis["properties"]["causal_summary"],
            "evidence_ids": hypothesis["properties"]["evidence_ids"],
        }
        hypothesis_required = list(hypothesis_schema)
        stop_schema = {
            "reason": reason,
            "stop_reason": stop["properties"]["stop_reason"],
            "considered_components": stop["properties"]["considered_components"],
            "considered_resources": stop["properties"]["considered_resources"],
            "missing_evidence_categories": stop["properties"]["missing_evidence_categories"],
        }
        stop_required = list(stop_schema)
        schemas = {
            "request_investigation_tools": object_schema(
                {
                    "reason": reason,
                    "tool_requests": {"type": "array", "items": request_items},
                },
                ["reason", "tool_requests"],
                request_definitions,
            ),
            "submit_causal_hypothesis": object_schema(
                hypothesis_schema,
                hypothesis_required,
                terminal_definitions,
            ),
            "stop_causal_investigation": object_schema(
                stop_schema,
                stop_required,
                terminal_definitions,
            ),
        }
    else:
        schemas = {
            "request_investigation_tools": object_schema(
                {
                    "reason": reason,
                    "tool_requests": {
                        "type": "array",
                        "items": request_items,
                    },
                },
                ["reason", "tool_requests"],
                request_definitions,
            ),
            "submit_root_cause_hypothesis": object_schema(
                {
                    "reason": reason,
                    "affected_component": hypothesis["properties"]["affected_component"],
                    "mechanism": hypothesis["properties"]["mechanism"],
                    "suspected_trigger": hypothesis["properties"]["suspected_trigger"],
                    "evidence_ids": hypothesis["properties"]["evidence_ids"],
                },
                [
                    "reason",
                    "affected_component",
                    "mechanism",
                    "suspected_trigger",
                    "evidence_ids",
                ],
                {
                    "HypothesisMechanism": definitions["HypothesisMechanism"],
                },
            ),
            "stop_investigation": object_schema(
                {
                    "reason": reason,
                    "stop_reason": {"type": "string", "enum": list(_STOP_REASON_VALUES)},
                },
                ["reason", "stop_reason"],
            ),
        }
    return {name: _compile_strict_schema(value) for name, value in schemas.items()}


def _itbench_decision_function_schemas(
    schema: dict[str, Any],
    allowed_tool_names: tuple[str, ...] | None = None,
    tool_schemas: tuple[ToolSchemaDescriptor, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build strict Responses functions for the external ITBench ontology."""
    compiled = _compile_strict_schema(schema)
    definitions = compiled.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("ITBench decision schema definitions are invalid")
    request = definitions.get("ToolRequestSpec")
    root_cause = (
        definitions.get("ExternalRootCauseV3")
        or definitions.get("ExternalRootCauseV2")
        or definitions.get("ExternalRootCause")
    )
    stop = definitions.get("ExternalStop")
    if not all(isinstance(item, dict) for item in (request, root_cause, stop)):
        raise ValueError("ITBench decision schema definitions are incomplete")
    assert isinstance(request, dict) and isinstance(root_cause, dict) and isinstance(stop, dict)

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    request_item: dict[str, Any] = request
    if tool_schemas is not None:
        branches: list[dict[str, Any]] = []
        allowed = set(allowed_tool_names) if allowed_tool_names is not None else None
        for descriptor in tool_schemas:
            if allowed is not None and descriptor.name not in allowed:
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            for key, field in descriptor.arguments.items():
                if not isinstance(field, dict):
                    continue
                projected = {
                    name: field[name]
                    for name in ("type", "enum", "minimum", "maximum", "minLength", "maxLength")
                    if name in field
                }
                if field.get("required") is False:
                    kind = projected.get("type", "string")
                    projected["type"] = [kind, "null"] if isinstance(kind, str) else kind
                properties[key] = projected
                required.append(key)
            branches.append(
                obj(
                    {
                        "tool": {"type": "string", "enum": [descriptor.name]},
                        "arguments": obj(properties, required),
                    },
                    ["tool", "arguments"],
                )
            )
        request_item = {"anyOf": branches}

    tool_requests = obj(
        {
            "reason": {"type": "string"},
            "tool_requests": {
                "type": "array",
                "items": request_item,
                "maxItems": 12,
            },
        },
        ["reason", "tool_requests"],
    )
    root_props = {
        "reason": {"type": "string"},
        "root_causes": {"type": "array", "items": root_cause, "maxItems": 5},
    }
    stop_schema = {
        "type": "object",
        "properties": stop["properties"],
        "required": list(stop["properties"]),
        "additionalProperties": False,
    }
    stop_props = {"reason": {"type": "string"}, "stop": stop_schema}
    result = {
        "request_itbench_tools": tool_requests,
        "submit_itbench_diagnosis": obj(root_props, ["reason", "root_causes"]),
        "stop_itbench_investigation": obj(stop_props, ["reason", "stop"]),
    }
    return {name: _compile_strict_schema(value) for name, value in result.items()}


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
    function_calls = [item for item in items if _field(item, "type") == "function_call"]
    function_call_names = [_field(item, "name") for item in function_calls]
    function_call_arguments = [_field(item, "arguments") for item in function_calls]
    output_texts = [
        _field(item, "text")
        for item in content_items
        if _field(item, "type") == "output_text"
        and isinstance(_field(item, "text"), str)
        and _field(item, "text") != ""
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
        function_call_count=len(function_calls),
        decision_function_call_count=sum(
            _field(item, "name")
            in (
                *DECISION_FUNCTION_NAMES,
                *A1_DECISION_FUNCTION_NAMES,
                *ITBENCH_DECISION_FUNCTION_NAMES,
            )
            for item in function_calls
        ),
        function_call_names=_safe_strings(function_call_names[:32]),
        function_call_argument_lengths=[
            len(item) for item in function_call_arguments[:32] if isinstance(item, str)
        ],
        function_call_argument_hashes=[
            sha256(item.encode("utf-8")).hexdigest()
            for item in function_call_arguments[:32]
            if isinstance(item, str)
        ],
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
    _validate_response_envelope(metadata)
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
            ProviderErrorCode.MULTIPLE_OUTPUT_TEXT_PAYLOADS,
            "assistant messages contained multiple output text payloads",
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


def _validate_response_envelope(metadata: ResponseEnvelopeMetadata) -> None:
    """Reject failed or incomplete Responses envelopes before extraction."""
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


def _extract_decision_function(
    request: ModelRequest,
    raw: Any,
) -> tuple[dict[str, Any], ResponseEnvelopeMetadata]:
    """Extract exactly one semantic decision function from a Responses envelope."""
    metadata = _response_metadata(raw)
    _validate_response_envelope(metadata)
    if metadata.refusal_item_count > 0:
        raise ProviderError(
            ProviderErrorCode.OUTPUT_REFUSAL,
            "provider response contained a refusal",
            metadata=metadata,
        )

    output = _field(raw, "output")
    items = output if isinstance(output, list) else []
    function_calls = [item for item in items if _field(item, "type") == "function_call"]
    a1_protocol = request.response_schema_name == "a1_investigation_decision"
    external_protocol = request.response_schema_name in {
        "itbench_investigation_decision_v1",
        "itbench_investigation_decision_v2",
        "itbench_investigation_decision_v3",
    }
    decision_names = (
        ITBENCH_DECISION_FUNCTION_NAMES
        if external_protocol
        else A1_DECISION_FUNCTION_NAMES
        if a1_protocol
        else DECISION_FUNCTION_NAMES
    )
    decision_calls = [item for item in function_calls if _field(item, "name") in decision_names]
    if len(decision_calls) > 1:
        raise ProviderError(
            ProviderErrorCode.MULTIPLE_DECISION_FUNCTION_CALLS,
            "provider response contained multiple decision function calls",
            metadata=metadata,
        )
    if len(function_calls) != len(decision_calls):
        raise ProviderError(
            ProviderErrorCode.UNEXPECTED_FUNCTION_CALL,
            "provider response contained an unexpected function call",
            metadata=metadata,
        )
    if not decision_calls:
        raise ProviderError(
            ProviderErrorCode.DECISION_FUNCTION_MISSING,
            "provider response contained no decision function call",
            metadata=metadata,
        )

    arguments = _field(decision_calls[0], "arguments")
    if not isinstance(arguments, str):
        raise ProviderError(
            ProviderErrorCode.FUNCTION_ARGUMENTS_INVALID_JSON,
            "decision function arguments were not a JSON string",
            metadata=metadata,
        )
    try:
        structured_output = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.FUNCTION_ARGUMENTS_INVALID_JSON,
            "decision function arguments were not valid JSON",
            metadata=metadata.model_copy(update={"json_error_position": error.pos}),
        ) from error
    if not isinstance(structured_output, dict):
        raise ProviderError(
            ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID,
            "decision function arguments were not an object",
            metadata=metadata,
        )
    function_name = _field(decision_calls[0], "name")
    decision_mapping = (
        ITBENCH_DECISION_TO_FUNCTION
        if external_protocol
        else A1_DECISION_TO_FUNCTION
        if a1_protocol
        else DECISION_TO_FUNCTION
    )
    allowed_functions = (
        tuple(decision_mapping[item] for item in request.allowed_decisions)
        if request.allowed_decisions is not None
        else decision_names
    )
    if function_name not in allowed_functions:
        raise ProviderError(
            ProviderErrorCode.UNEXPECTED_FUNCTION_CALL,
            "provider response used a decision function not exposed for this turn",
            metadata=metadata,
        )
    schemas = (
        _itbench_decision_function_schemas(
            request.response_schema, request.allowed_tool_names, request.tool_schemas
        )
        if external_protocol
        else _decision_function_schemas(
            request.response_schema,
            request.allowed_tool_names,
            request.tool_schemas,
            a1_protocol=a1_protocol,
        )
    )
    schema_error_path = _json_schema_error(structured_output, schemas[function_name])
    if schema_error_path is not None:
        error_code = (
            ProviderErrorCode.INVALID_STOP_REASON
            if function_name in {"stop_investigation", "stop_causal_investigation"}
            and schema_error_path == "$.stop_reason"
            else ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID
        )
        raise ProviderError(
            error_code,
            "decision function arguments did not match response schema",
            metadata=metadata.model_copy(update={"schema_error_path": schema_error_path}),
        )
    if function_name == "request_itbench_tools":
        return {
            "decision": "CALL_TOOLS",
            "requests": _normalize_tool_requests(structured_output["tool_requests"]),
            "root_causes": [],
            "stop": None,
        }, metadata
    if function_name == "request_investigation_tools":
        return {
            "decision": "CALL_TOOLS",
            "requests": _normalize_tool_requests(structured_output["tool_requests"]),
            "hypothesis": None,
        }, metadata
    if function_name == "submit_itbench_diagnosis":
        return {
            "decision": "SUBMIT_DIAGNOSIS",
            "requests": [],
            "root_causes": structured_output["root_causes"],
            "stop": None,
        }, metadata
    if function_name in {"submit_root_cause_hypothesis", "submit_causal_hypothesis"}:
        if a1_protocol:
            return {
                "decision": "SUBMIT_HYPOTHESIS",
                "requests": [],
                "hypothesis": {
                    key: structured_output[key]
                    for key in (
                        "symptom_component",
                        "causal_component",
                        "causal_resource",
                        "mechanism",
                        "structured_trigger",
                        "causal_summary",
                        "evidence_ids",
                    )
                },
                "stop": None,
            }, metadata
        return {
            "decision": "SUBMIT_HYPOTHESIS",
            "requests": [],
            "hypothesis": {
                key: structured_output[key]
                for key in (
                    "affected_component",
                    "mechanism",
                    "suspected_trigger",
                    "evidence_ids",
                )
            },
        }, metadata
    if function_name == "stop_itbench_investigation":
        return {
            "decision": "STOP",
            "requests": [],
            "root_causes": [],
            "stop": structured_output["stop"],
        }, metadata
    if function_name in {"stop_investigation", "stop_causal_investigation"}:
        if a1_protocol:
            return {
                "decision": "STOP",
                "requests": [],
                "hypothesis": None,
                "stop": {
                    key: structured_output[key]
                    for key in (
                        "stop_reason",
                        "considered_components",
                        "considered_resources",
                        "missing_evidence_categories",
                    )
                },
            }, metadata
        return {
            "decision": "STOP",
            "requests": [],
            "hypothesis": None,
            "stop_reason": structured_output["stop_reason"],
        }, metadata
    raise ProviderError(
        ProviderErrorCode.UNEXPECTED_FUNCTION_CALL,
        "provider response contained an unexpected decision function",
        metadata=metadata,
    )


def _normalize_tool_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize nullable wire arguments without selecting a protocol envelope."""
    return [
        {
            **request,
            "arguments": {
                key: value
                for key, value in request.get("arguments", {}).items()
                if value is not None
            },
        }
        for request in requests
    ]


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
        if self._config.enabled:
            try:
                validate_agent_config(self._config.model, self._config.reasoning_effort)
            except ModelPolicyError as error:
                raise ProviderError(
                    ProviderErrorCode.BENCHMARK_MODEL_POLICY_VIOLATION,
                    str(error),
                ) from error
        self._transport = transport or self._build_transport()
        self._max_retry = max_retry
        self._accounting_lock = Lock()
        self._provider_invocations = 0
        self._outbound_api_attempts = 0
        self._provider_retries = 0
        self._shared_ledger_consumed = 0

    def accounting_snapshot(self) -> ProviderAccountingSnapshot:
        """Return non-secret counters for provider and outbound API activity."""
        with self._accounting_lock:
            return ProviderAccountingSnapshot(
                provider_invocations=self._provider_invocations,
                outbound_api_attempts=self._outbound_api_attempts,
                provider_retries=self._provider_retries,
                shared_ledger_consumed=self._shared_ledger_consumed,
            )

    def _request_parameters(self, request: ModelRequest) -> dict[str, Any]:
        """Build the request payload before reserving any live budget."""
        parameters: dict[str, Any] = {
            "model": request.model,
            "input": messages_to_dicts(request.messages),
            "reasoning": {"effort": request.reasoning_effort},
            "max_output_tokens": request.max_output_tokens,
            "timeout": request.timeout_ms / 1000,
        }
        if request.response_schema_name in {
            "investigation_decision",
            "a1_investigation_decision",
            "itbench_investigation_decision_v1",
            "itbench_investigation_decision_v2",
            "itbench_investigation_decision_v3",
        }:
            a1_protocol = request.response_schema_name == "a1_investigation_decision"
            external_protocol = request.response_schema_name in {
                "itbench_investigation_decision_v1",
                "itbench_investigation_decision_v2",
                "itbench_investigation_decision_v3",
            }
            schemas = (
                _itbench_decision_function_schemas(
                    request.response_schema,
                    request.allowed_tool_names,
                    request.tool_schemas,
                )
                if external_protocol
                else _decision_function_schemas(
                    request.response_schema,
                    request.allowed_tool_names,
                    request.tool_schemas,
                    a1_protocol=a1_protocol,
                )
            )
            decision_mapping = (
                ITBENCH_DECISION_TO_FUNCTION
                if external_protocol
                else A1_DECISION_TO_FUNCTION
                if a1_protocol
                else DECISION_TO_FUNCTION
            )
            decision_names = (
                ITBENCH_DECISION_FUNCTION_NAMES
                if external_protocol
                else A1_DECISION_FUNCTION_NAMES
                if a1_protocol
                else DECISION_FUNCTION_NAMES
            )
            descriptions = (
                A1_DECISION_FUNCTION_DESCRIPTIONS if a1_protocol else DECISION_FUNCTION_DESCRIPTIONS
            )
            if external_protocol:
                descriptions = {
                    **DECISION_FUNCTION_DESCRIPTIONS,
                    "request_itbench_tools": "Request bounded read-only ITBench evidence.",
                    "submit_itbench_diagnosis": "Submit supported ITBench root-cause entities using namespace/Kind/name and runtime-issued evidence handles.",
                    "stop_itbench_investigation": "Stop when observable evidence cannot support a reliable diagnosis.",
                }
            allowed = (
                tuple(decision_mapping[item] for item in request.allowed_decisions)
                if request.allowed_decisions is not None
                else decision_names
            )
            unknown = set(allowed) - set(decision_names)
            if unknown:
                raise ProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "request contained an unknown decision function",
                )
            parameters.update(
                {
                    "tools": [
                        {
                            "type": "function",
                            "name": name,
                            "description": descriptions[name],
                            "parameters": schema,
                            "strict": True,
                        }
                        for name, schema in schemas.items()
                        if name in allowed
                    ],
                    "parallel_tool_calls": False,
                    "tool_choice": "required",
                }
            )
        else:
            parameters["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.response_schema_name,
                    "schema": _compile_strict_schema(request.response_schema),
                    "strict": True,
                }
            }
        return parameters

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

        parameters = self._request_parameters(request)
        with self._accounting_lock:
            self._provider_invocations += 1
        attempts = self._max_retry + 1
        for attempt in range(attempts):
            self._budget.consume()
            with self._accounting_lock:
                self._shared_ledger_consumed += 1
                self._outbound_api_attempts += 1
                if attempt > 0:
                    self._provider_retries += 1
            started = monotonic()
            try:
                raw = self._transport.create(**parameters)
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
        if request.response_schema_name in {
            "investigation_decision",
            "a1_investigation_decision",
            "itbench_investigation_decision_v1",
            "itbench_investigation_decision_v2",
            "itbench_investigation_decision_v3",
        }:
            structured_output, metadata = _extract_decision_function(request, raw)
            schema_error_path = _json_schema_error(structured_output, request.response_schema)
            if schema_error_path is not None:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "normalized provider decision did not match the declared response schema",
                    metadata=metadata.model_copy(update={"schema_error_path": schema_error_path}),
                )
        else:
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
