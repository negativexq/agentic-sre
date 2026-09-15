"""Provider/runtime parity tests for the E8 fixed-slot external protocol."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.evals.itbench.external_contracts import (
    ITBenchInvestigationDecisionV4,
)
from packages.provider import ModelMessage, ModelRequest, OpenAIProvider, ToolSchemaDescriptor
from packages.provider.openai import _itbench_decision_function_schemas, _json_schema_error


def _request() -> ModelRequest:
    schema = ITBenchInvestigationDecisionV4.model_json_schema()
    return ModelRequest(
        run_id=uuid4(),
        messages=[ModelMessage(role="user", content="test")],
        response_schema_name="itbench_investigation_decision_v4",
        response_schema=schema,
        model="gpt-5.6-luna",
        reasoning_effort="none",
        max_output_tokens=100,
        timeout_ms=1000,
        allowed_decisions=("CALL_TOOLS", "SUBMIT_DIAGNOSIS", "STOP"),
        allowed_tool_names=("itbench_logs",),
        tool_schemas=(
            ToolSchemaDescriptor(
                name="itbench_logs",
                arguments={
                    "contains": {"type": "string", "required": False},
                    "limit": {"type": "integer", "required": True},
                },
            ),
        ),
    )


def _wire_request(*, primary: object = None, second: object = None) -> dict[str, object]:
    return {
        "reason": "inspect",
        "primary_request": primary,
        "additional_request_2": second,
        "additional_request_3": None,
        "candidate_update_1": None,
        "candidate_update_2": None,
        "candidate_update_3": None,
    }


def _slot() -> dict[str, object]:
    return {"tool": "itbench_logs", "arguments": {"contains": None, "limit": 1}}


def _raw(function: str, arguments: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        id="e8-response",
        status="completed",
        error=None,
        incomplete_details=None,
        output=[
            SimpleNamespace(type="function_call", name=function, arguments=json.dumps(arguments))
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def test_wire_schema_requires_primary_slot_and_allows_only_three() -> None:
    schemas = _itbench_decision_function_schemas(
        ITBenchInvestigationDecisionV4.model_json_schema(),
        ("itbench_logs",),
        _request().tool_schemas,
    )
    request_schema = schemas["request_itbench_tools"]
    assert "primary_request" in request_schema["required"]
    assert "additional_request_3" in request_schema["required"]
    assert _json_schema_error(_wire_request(primary=_slot()), request_schema) is None
    assert _json_schema_error(_wire_request(), request_schema) == "$.primary_request"
    too_many = _wire_request(primary=_slot(), second=_slot())
    too_many["additional_request_3"] = _slot()
    assert _json_schema_error(too_many, request_schema) is None
    assert "additional_request_4" not in request_schema["properties"]


def test_provider_normalizes_wire_v4_to_local_v4() -> None:
    request = _request()
    raw = _raw("request_itbench_tools", _wire_request(primary=_slot()))
    normalized = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(request, raw, 0.0)
    decision = ITBenchInvestigationDecisionV4.model_validate_json(
        json.dumps(normalized.structured_output)
    )
    assert decision.decision.value == "CALL_TOOLS"
    assert [item.tool for item in decision.requests] == ["itbench_logs"]


def test_v4_zero_cardinality_is_rejected_locally_for_all_semantics() -> None:
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV4.model_validate_json(
            json.dumps(
                {
                    "decision": "CALL_TOOLS",
                    "primary_request": None,
                    "additional_request_2": None,
                    "additional_request_3": None,
                }
            )
        )
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV4.model_validate_json(
            json.dumps(
                {
                    "decision": "SUBMIT_DIAGNOSIS",
                    "primary_root_cause": None,
                }
            )
        )
    stop = ITBenchInvestigationDecisionV4.model_validate_json(
        json.dumps(
            {
                "decision": "STOP",
                "stop": {
                    "stop_reason": "insufficient_evidence",
                    "evidence_categories_considered": [],
                    "entities_considered": [],
                    "missing_evidence_categories": [],
                },
            }
        )
    )
    assert stop.stop is not None


def test_v4_cross_decision_payloads_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV4.model_validate_json(
            json.dumps(
                {
                    "decision": "CALL_TOOLS",
                    "primary_request": _slot(),
                    "primary_root_cause": {
                        "entity": "demo/Pod/a",
                        "causal_summary": "cause",
                        "evidence_refs": ["E001"],
                    },
                },
            )
        )
