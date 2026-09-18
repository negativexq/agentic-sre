"""Provider-neutral domain and strict OpenAI wire-contract tests."""

from __future__ import annotations

from typing import Any

import pytest

from packages.rca.investigation.policy import (
    ACTION_SCHEMA,
    InvestigationActionWire,
    validate_strict_json_schema,
)
from packages.rca.llm import OpenAIClient, ProviderRequestError


def _inspect_payload() -> dict[str, Any]:
    return {
        "action": "inspect",
        "gap_id": "gap:test",
        "capability": "events",
        "target": {"kind": "Pod", "name": "demo", "namespace": "default"},
        "rationale": "inspect the allowed event stream",
    }


def test_investigation_wire_schema_is_openai_strict() -> None:
    validate_strict_json_schema(ACTION_SCHEMA)
    assert set(ACTION_SCHEMA["required"]) == {
        "action",
        "gap_id",
        "capability",
        "target",
        "rationale",
    }
    target = ACTION_SCHEMA["$defs"]["InvestigationTargetWire"]
    assert set(target["required"]) == {"kind", "name", "namespace"}
    assert target["additionalProperties"] is False


def test_wire_inspect_converts_to_domain_action() -> None:
    action = InvestigationActionWire.model_validate(_inspect_payload()).to_domain()
    assert action.action == "inspect"
    assert action.gap_id == "gap:test"
    assert action.capability == "events"
    assert action.target is not None
    assert action.target.canonical == "default/Pod/demo"


def test_wire_stop_requires_explicit_nulls() -> None:
    action = InvestigationActionWire.model_validate(
        {
            "action": "stop",
            "gap_id": None,
            "capability": None,
            "target": None,
            "rationale": "no useful observation remains",
        }
    ).to_domain()
    assert action.action == "stop"
    assert action.gap_id is None and action.capability is None and action.target is None


def test_wire_fields_and_nested_target_reject_unknowns() -> None:
    with pytest.raises(ValueError):
        InvestigationActionWire.model_validate({**_inspect_payload(), "extra": True})
    with pytest.raises(ValueError):
        InvestigationActionWire.model_validate(
            {**_inspect_payload(), "target": {**_inspect_payload()["target"], "extra": True}}
        )
    with pytest.raises(ValueError):
        InvestigationActionWire.model_validate(
            {key: value for key, value in _inspect_payload().items() if key != "rationale"}
        )


def test_provider_request_error_keeps_sanitized_details() -> None:
    class FakeBadRequest(Exception):
        status_code = 400
        code = "invalid_request_error"
        param = "text.format.schema"
        request_id = "req_test_123"
        message = "Bad schema sk-secret-value"

    class Responses:
        def create(self, **_: Any) -> Any:
            raise FakeBadRequest()

    class SDK:
        responses = Responses()

    client = OpenAIClient(enabled=True, max_calls=1, client=SDK())
    with pytest.raises(ProviderRequestError) as raised:
        client.complete_json(system="s", user="u", schema=ACTION_SCHEMA, name="action")
    details = raised.value.details
    assert details.category == "PROVIDER_REQUEST_ERROR"
    assert details.status_code == 400
    assert details.code == "invalid_request_error"
    assert details.parameter == "text.format.schema"
    assert details.request_id == "req_test_123"
    assert "sk-secret-value" not in details.message
    assert "[REDACTED]" in details.message
