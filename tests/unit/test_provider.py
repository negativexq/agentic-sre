"""Offline provider contracts and credit budget tests."""

import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.investigation import InvestigationDecision
from packages.investigation.registry import live_observability_registry
from packages.provider import (
    FakeModelProvider,
    LiveModelBudget,
    ModelMessage,
    ModelRequest,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCode,
    ToolSchemaDescriptor,
)
from packages.provider.openai import (
    DECISION_FUNCTION_DESCRIPTIONS,
    DECISION_FUNCTION_NAMES,
    LiveModelConfig,
    _compile_strict_schema,
    _decision_function_schemas,
)


def request() -> ModelRequest:
    """Build a minimal structured-output request for provider tests."""
    return ModelRequest(
        run_id=uuid4(),
        messages=[ModelMessage(role="user", content="return the scripted decision")],
        response_schema_name="decision",
        response_schema={"type": "object"},
        model="gpt-5.6-luna",
        reasoning_effort="none",
        max_output_tokens=100,
        timeout_ms=1_000,
    )


def function_request() -> ModelRequest:
    """Build an investigation request that uses the forced decision function."""
    return request().model_copy(
        update={
            "response_schema_name": "investigation_decision",
            "response_schema": InvestigationDecision.model_json_schema(),
        }
    )


def registry_function_request() -> ModelRequest:
    """Build an investigation request with canonical per-tool schemas."""
    registry = live_observability_registry("prometheus", "loki", "tempo")
    return function_request().model_copy(
        update={
            "allowed_tool_names": registry.names(),
            "tool_schemas": tuple(
                ToolSchemaDescriptor(name=item["name"], arguments=item["arguments"])
                for item in registry.descriptors()
            ),
        }
    )


def function_arguments(name: str) -> str:
    """Build valid arguments for one semantic decision transport function."""
    if name == "request_investigation_tools":
        return json.dumps(
            {
                "reason": "latency evidence is needed",
                "tool_requests": [{"tool": "service_latency", "arguments": {}}],
            }
        )
    if name == "submit_root_cause_hypothesis":
        return json.dumps(
            {
                "reason": "the available evidence is sufficient",
                "affected_component": "payment-service",
                "mechanism": "service_latency_regression",
                "suspected_trigger": "elevated latency",
                "evidence_ids": [str(uuid4())],
            }
        )
    return json.dumps(
        {"reason": "no more useful evidence is available", "stop_reason": "insufficient_evidence"}
    )


def _reserve_budget_worker(path: str) -> None:
    """Reserve one shared budget unit from a child process."""
    LiveModelBudget(2, ledger_path=path).consume()


def test_fake_provider_is_deterministic_and_records_requests() -> None:
    """Fake responses are returned without any network dependency."""
    provider = FakeModelProvider([{"decision": "STOP"}])

    response = provider.complete(request())

    assert response.provider == "fake"
    assert response.structured_output == {"decision": "STOP"}
    assert len(provider.requests) == 1


def test_live_budget_blocks_request_before_transport() -> None:
    """The third request is stopped locally when the budget is two."""
    calls = 0

    class Transport:
        def create(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return type("Response", (), {"output_text": '{"decision":"STOP"}'})()

    provider = OpenAIProvider(
        budget=LiveModelBudget(2),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
    )

    provider.complete(request())
    provider.complete(request())
    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED
    assert calls == 2


def test_live_budget_preflight_rejects_insufficient_capacity() -> None:
    """A benchmark can refuse to start before consuming any call budget."""
    budget = LiveModelBudget(2)

    budget.consume()
    with pytest.raises(ProviderError) as error:
        budget.ensure_capacity(2)

    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED
    assert budget.snapshot().calls_used == 1


def test_live_budget_can_share_a_call_ledger_between_process_boundaries(tmp_path: Path) -> None:
    """Separate explicit live commands can share one counter without secrets."""
    ledger = tmp_path / "budget.json"
    first = LiveModelBudget(2, ledger_path=str(ledger))
    second = LiveModelBudget(2, ledger_path=str(ledger))

    first.consume()
    second.consume()
    with pytest.raises(ProviderError) as error:
        first.consume()

    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED
    assert ledger.read_text(encoding="utf-8") == '{"calls_used": 2}'


def test_live_provider_is_disabled_by_default() -> None:
    """A passed transport cannot bypass the explicit disabled flag."""
    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=False),
        transport=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is ProviderErrorCode.LIVE_MODEL_DISABLED


def test_live_provider_allows_one_explicit_transient_retry() -> None:
    """Only one retry is made for a transient 5xx response."""
    calls = 0

    class TransientError(RuntimeError):
        status_code = 503

    class Transport:
        def create(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientError()
            return type("Response", (), {"output_text": '{"decision":"STOP"}'})()

    provider = OpenAIProvider(
        budget=LiveModelBudget(2),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
    )

    response = provider.complete(request())

    assert response.structured_output == {"decision": "STOP"}
    assert calls == 2


def test_live_provider_accounting_counts_retry_and_ledger_units() -> None:
    """Provider retries are outbound attempts and consume shared units."""
    calls = 0

    class TransientError(RuntimeError):
        status_code = 503

    class Transport:
        def create(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientError()
            return type("Response", (), {"output_text": '{"decision":"STOP"}'})()

    provider = OpenAIProvider(
        budget=LiveModelBudget(2),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
    )

    provider.complete(request())
    accounting = provider.accounting_snapshot()

    assert accounting.provider_invocations == 1
    assert accounting.outbound_api_attempts == 2
    assert accounting.provider_retries == 1
    assert accounting.shared_ledger_consumed == 2


def test_local_request_schema_failure_consumes_no_budget_or_transport_call() -> None:
    """Invalid local function schema is rejected before an outbound attempt."""
    calls = 0

    class Transport:
        def create(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("transport must not be called")

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )
    invalid_request = function_request().model_copy(update={"response_schema": []})

    with pytest.raises(ValueError):
        provider.complete(invalid_request)

    accounting = provider.accounting_snapshot()
    assert calls == 0
    assert accounting.outbound_api_attempts == 0
    assert accounting.shared_ledger_consumed == 0


def test_provider_parse_failure_consumes_one_attempt() -> None:
    """A response parsing failure still represents one consumed API attempt."""

    class Transport:
        def create(self, **_kwargs: object) -> object:
            return type("Response", (), {"output_text": '{"decision":"STOP"}{"decision":"STOP"}'})()

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is ProviderErrorCode.JSON_DECODE_FAILED
    accounting = provider.accounting_snapshot()
    assert accounting.outbound_api_attempts == 1
    assert accounting.shared_ledger_consumed == 1


def test_semantic_invalid_decision_consumes_one_attempt() -> None:
    """A schema-valid but semantically empty tool request is still charged once."""

    class Transport:
        def create(self, **_kwargs: object) -> object:
            return _envelope(
                [
                    _function_call(
                        json.dumps({"reason": "missing evidence", "tool_requests": []}),
                        name="request_investigation_tools",
                    )
                ]
            )

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    response = provider.complete(function_request())
    with pytest.raises(ValueError):
        InvestigationDecision.model_validate(response.structured_output)

    accounting = provider.accounting_snapshot()
    assert accounting.outbound_api_attempts == 1
    assert accounting.shared_ledger_consumed == 1


def test_two_processes_reserve_final_shared_budget_units(tmp_path: Path) -> None:
    """The file lock prevents concurrent processes from exceeding the cap."""
    ledger = tmp_path / "budget.json"
    ledger.write_text('{"calls_used": 0}', encoding="utf-8")
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_reserve_budget_worker, args=(str(ledger),)) for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert LiveModelBudget(2, ledger_path=str(ledger)).snapshot().calls_used == 2


def test_live_provider_retries_connection_reset_once() -> None:
    """A connection reset is transient, but it still consumes another budget slot."""
    calls = 0

    class Transport:
        def create(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("connection reset")
            return type("Response", (), {"output_text": '{"decision":"STOP"}'})()

    provider = OpenAIProvider(
        budget=LiveModelBudget(2),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
    )

    provider.complete(request())

    assert calls == 2


def test_responses_schema_compiler_emits_strict_provider_subset() -> None:
    """Pydantic validation constraints do not invalidate strict wire schemas."""
    schema = _compile_strict_schema(InvestigationDecision.model_json_schema())

    def assert_strict(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                assert_strict(item)
        if not isinstance(node, dict):
            return
        for keyword in (
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
        ):
            assert keyword not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            properties = node.get("properties", {})
            assert node["required"] == list(properties)
        for value in node.values():
            assert_strict(value)

    assert_strict(schema)


def test_responses_parser_uses_one_output_segment_not_aggregate_text() -> None:
    """A concatenated SDK convenience field cannot corrupt valid output."""
    aggregate = '{"decision":"STOP"}{"decision":"STOP"}'
    response = SimpleNamespace(
        output_text=aggregate,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"decision":"STOP"}')],
            )
        ],
    )

    class Transport:
        def create(self, **_kwargs: object) -> object:
            return response

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    result = provider.complete(request())

    assert result.structured_output == {"decision": "STOP"}


def test_responses_parser_rejects_multiple_structured_payloads() -> None:
    """Multiple output payloads fail closed instead of being concatenated."""
    response = SimpleNamespace(
        output_text='{"decision":"STOP"}{"decision":"STOP"}',
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(type="output_text", text='{"decision":"STOP"}'),
                    SimpleNamespace(type="output_text", text='{"decision":"STOP"}'),
                ],
            )
        ],
    )

    class Transport:
        def create(self, **_kwargs: object) -> object:
            return response

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is ProviderErrorCode.MULTIPLE_OUTPUT_TEXT_PAYLOADS


def _envelope(
    output: list[object],
    *,
    status: str = "completed",
    error: object | None = None,
    incomplete_details: object | None = None,
    output_text: str | None = None,
) -> SimpleNamespace:
    """Build a safe Responses envelope fixture without raw provider payloads."""
    return SimpleNamespace(
        id="resp_fixture",
        status=status,
        error=error,
        incomplete_details=incomplete_details,
        output=output,
        output_text=output_text,
    )


def _message(*content: object) -> SimpleNamespace:
    """Build one assistant message fixture."""
    return SimpleNamespace(type="message", content=list(content))


def _output_text(value: str) -> SimpleNamespace:
    """Build one structured output content fixture."""
    return SimpleNamespace(type="output_text", text=value)


def _function_call(
    arguments: str,
    *,
    name: str = "stop_investigation",
) -> SimpleNamespace:
    """Build one Responses function-call output item."""
    return SimpleNamespace(type="function_call", name=name, arguments=arguments)


def _normalize_fixture(raw: SimpleNamespace, model_request: ModelRequest | None = None) -> object:
    """Normalize a response fixture without invoking the transport."""
    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=object(),  # type: ignore[arg-type]
        max_retry=0,
    )
    return provider._normalize_response(model_request or request(), raw, 0.0)


def test_responses_request_exposes_only_three_decision_functions() -> None:
    """Investigation decisions expose only semantic transport functions."""
    captured: dict[str, object] = {}

    class Transport:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return _envelope([_function_call(function_arguments("stop_investigation"))])

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    response = provider.complete(function_request())

    assert response.structured_output == {
        "decision": "STOP",
        "requests": [],
        "hypothesis": None,
        "stop_reason": "insufficient_evidence",
    }
    assert captured["parallel_tool_calls"] is False
    assert captured["tool_choice"] == "required"
    tools = captured["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == list(DECISION_FUNCTION_NAMES)
    assert all(tool["type"] == "function" for tool in tools)
    assert all(tool["strict"] is True for tool in tools)
    assert all(tool["parameters"]["additionalProperties"] is False for tool in tools)


def test_provider_request_schema_is_specific_to_each_registered_tool() -> None:
    """The provider schema derives each argument object from its registered tool."""
    request = registry_function_request()
    schemas = _decision_function_schemas(
        request.response_schema,
        request.allowed_tool_names,
        request.tool_schemas,
    )
    items = schemas["request_investigation_tools"]["properties"]["tool_requests"]["items"]
    branches = items["anyOf"]
    assert len(branches) == 17
    by_name = {branch["properties"]["tool"]["enum"][0]: branch for branch in branches}
    assert set(by_name) == set(request.allowed_tool_names or ())
    assert set(by_name["service_logs"]["properties"]["arguments"]["properties"]) == {
        "service",
        "range_seconds",
    }
    assert set(by_name["service_error_logs"]["properties"]["arguments"]["properties"]) == {
        "service",
        "pattern",
    }
    assert by_name["service_logs"]["properties"]["arguments"]["properties"]["range_seconds"][
        "type"
    ] == ["integer", "null"]

    for descriptor in request.tool_schemas or ():
        branch = by_name[descriptor.name]
        arguments = branch["properties"]["arguments"]
        assert arguments["additionalProperties"] is False
        assert arguments["required"] == list(descriptor.arguments)
        assert set(arguments["properties"]) == set(descriptor.arguments)
        for field_name, field_schema in descriptor.arguments.items():
            provider_schema = arguments["properties"][field_name]
            expected_type = field_schema["type"]
            if field_schema["required"] is False and isinstance(expected_type, str):
                expected_type = [expected_type, "null"]
            assert provider_schema["type"] == expected_type


def test_provider_rejects_cross_tool_argument_leakage_before_runtime() -> None:
    """A service_logs request cannot use service_error_logs-only arguments."""
    request = registry_function_request()
    raw = _envelope(
        [
            _function_call(
                json.dumps(
                    {
                        "reason": "inspect logs",
                        "tool_requests": [
                            {
                                "tool": "service_logs",
                                "arguments": {"service": "order-worker", "pattern": "ERROR"},
                            }
                        ],
                    }
                ),
                name="request_investigation_tools",
            )
        ]
    )
    with pytest.raises(ProviderError) as error:
        _normalize_fixture(raw, request)
    assert error.value.code is ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID


def test_provider_accepts_fields_for_their_canonical_tool_only() -> None:
    """A service_error_logs pattern remains valid in its own schema branch."""
    request = registry_function_request()
    raw = _envelope(
        [
            _function_call(
                json.dumps(
                    {
                        "reason": "inspect error logs",
                        "tool_requests": [
                            {
                                "tool": "service_error_logs",
                                "arguments": {"service": "order-worker", "pattern": "ERROR"},
                            }
                        ],
                    }
                ),
                name="request_investigation_tools",
            )
        ]
    )
    normalized = _normalize_fixture(raw, request)
    assert normalized.structured_output["requests"][0]["arguments"] == {  # type: ignore[attr-defined]
        "service": "order-worker",
        "pattern": "ERROR",
    }


def test_final_turn_exposes_only_terminal_decision_functions() -> None:
    """A final model turn cannot request evidence without a future interpreter."""
    captured: dict[str, object] = {}

    class Transport:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return _envelope([_function_call(function_arguments("stop_investigation"))])

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )
    provider.complete(
        function_request().model_copy(update={"allowed_decisions": ("SUBMIT_HYPOTHESIS", "STOP")})
    )

    tools = captured["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == [
        "submit_root_cause_hypothesis",
        "stop_investigation",
    ]


def test_decision_function_descriptions_are_semantically_distinct() -> None:
    """Each provider transport function describes its own decision meaning."""
    assert "additional" in DECISION_FUNCTION_DESCRIPTIONS["request_investigation_tools"]
    assert "terminal" in DECISION_FUNCTION_DESCRIPTIONS["submit_root_cause_hypothesis"]
    assert "terminate" in DECISION_FUNCTION_DESCRIPTIONS["stop_investigation"]
    assert len(set(DECISION_FUNCTION_DESCRIPTIONS.values())) == len(DECISION_FUNCTION_NAMES)


@pytest.mark.parametrize(
    "output",
    [
        [_function_call(function_arguments("stop_investigation"))],
        [
            SimpleNamespace(type="reasoning", summary=[]),
            _function_call(function_arguments("stop_investigation")),
        ],
        [
            _message(_output_text("harmless assistant commentary")),
            _function_call(function_arguments("stop_investigation")),
        ],
    ],
)
def test_decision_function_accepts_non_decision_output_items(output: list[object]) -> None:
    """Reasoning and message items do not become decision transport payloads."""
    response = _normalize_fixture(_envelope(output), function_request())

    assert response.structured_output == {  # type: ignore[attr-defined]
        "decision": "STOP",
        "requests": [],
        "hypothesis": None,
        "stop_reason": "insufficient_evidence",
    }
    assert response.response_metadata is not None  # type: ignore[attr-defined]
    assert response.response_metadata.decision_function_call_count == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("function_name", "decision"),
    [
        ("request_investigation_tools", "CALL_TOOLS"),
        ("submit_root_cause_hypothesis", "SUBMIT_HYPOTHESIS"),
        ("stop_investigation", "STOP"),
    ],
)
def test_decision_functions_map_to_provider_independent_decisions(
    function_name: str, decision: str
) -> None:
    """Each function identity maps to the existing InvestigationDecision shape."""
    response = _normalize_fixture(
        _envelope([_function_call(function_arguments(function_name), name=function_name)]),
        function_request(),
    )

    assert response.structured_output["decision"] == decision  # type: ignore[attr-defined]
    assert "reason" not in response.structured_output  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ([], ProviderErrorCode.DECISION_FUNCTION_MISSING),
        (
            [
                _function_call(function_arguments("stop_investigation")),
                _function_call(function_arguments("stop_investigation")),
            ],
            ProviderErrorCode.MULTIPLE_DECISION_FUNCTION_CALLS,
        ),
        (
            [_function_call(function_arguments("stop_investigation"), name="other_function")],
            ProviderErrorCode.UNEXPECTED_FUNCTION_CALL,
        ),
        (
            [_function_call('{"decision":')],
            ProviderErrorCode.FUNCTION_ARGUMENTS_INVALID_JSON,
        ),
        (
            [_function_call('{"reason":"x","stop_reason":"bad"}')],
            ProviderErrorCode.INVALID_STOP_REASON,
        ),
        (
            [_function_call('{"decision":"STOP"}')],
            ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID,
        ),
        (
            [_message(SimpleNamespace(type="refusal", refusal="not available"))],
            ProviderErrorCode.OUTPUT_REFUSAL,
        ),
    ],
)
def test_decision_function_failure_taxonomy(
    output: list[object], expected: ProviderErrorCode
) -> None:
    """Function-call extraction rejects every ambiguous or malformed envelope."""
    with pytest.raises(ProviderError) as error:
        _normalize_fixture(_envelope(output), function_request())

    assert error.value.code is expected
    assert error.value.metadata is not None
    assert error.value.metadata.response_id == "resp_fixture"


def test_decision_function_rejects_incomplete_response_before_extraction() -> None:
    """Incomplete Responses never reach function argument parsing."""
    raw = _envelope(
        [_function_call(function_arguments("stop_investigation"))],
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )

    with pytest.raises(ProviderError) as error:
        _normalize_fixture(raw, function_request())

    assert error.value.code is ProviderErrorCode.RESPONSE_INCOMPLETE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            _envelope([], status="incomplete", incomplete_details=SimpleNamespace(reason="length")),
            ProviderErrorCode.RESPONSE_INCOMPLETE,
        ),
        (
            _envelope(
                [],
                status="failed",
                error=SimpleNamespace(type="server_error", code="response_failed", param=None),
            ),
            ProviderErrorCode.RESPONSE_ERROR,
        ),
        (_envelope([]), ProviderErrorCode.OUTPUT_MESSAGE_MISSING),
        (
            _envelope(
                [
                    SimpleNamespace(type="reasoning", summary=[]),
                    _message(_output_text('{"decision":"STOP"}')),
                ]
            ),
            None,
        ),
        (
            _envelope(
                [
                    SimpleNamespace(type="reasoning", summary=[]),
                    _message(),
                    _message(_output_text('{"decision":"STOP"}')),
                ]
            ),
            None,
        ),
        (
            _envelope(
                [
                    _message(_output_text('{"decision":"STOP"}')),
                    _message(),
                ]
            ),
            None,
        ),
        (
            _envelope(
                [
                    SimpleNamespace(type="web_search_call", status="completed"),
                    _message(_output_text('{"decision":"STOP"}')),
                ]
            ),
            None,
        ),
        (
            _envelope([_message(SimpleNamespace(type="refusal", refusal="not available"))]),
            ProviderErrorCode.OUTPUT_REFUSAL,
        ),
        (
            _envelope([_message(SimpleNamespace(type="input_text", text="not structured"))]),
            ProviderErrorCode.OUTPUT_TEXT_MISSING,
        ),
        (
            _envelope(
                [
                    _message(
                        _output_text('{"decision":"STOP"}'),
                        _output_text('{"decision":"STOP"}'),
                    )
                ]
            ),
            ProviderErrorCode.MULTIPLE_OUTPUT_TEXT_PAYLOADS,
        ),
        (
            _envelope(
                [
                    _message(_output_text('{"decision":"STOP"}')),
                    _message(_output_text('{"decision":"STOP"}')),
                ]
            ),
            ProviderErrorCode.MULTIPLE_OUTPUT_TEXT_PAYLOADS,
        ),
        (
            _envelope([_message(), _message()]),
            ProviderErrorCode.OUTPUT_TEXT_MISSING,
        ),
        (
            _envelope(
                [
                    _message(
                        _output_text('{"decision":"STOP"}'),
                        SimpleNamespace(type="refusal", refusal="not available"),
                    )
                ]
            ),
            ProviderErrorCode.OUTPUT_REFUSAL,
        ),
        (
            _envelope([_message(_output_text('{"decision":"STOP"}{"decision":"STOP"}'))]),
            ProviderErrorCode.JSON_DECODE_FAILED,
        ),
    ],
)
def test_responses_envelope_failure_taxonomy(
    raw: SimpleNamespace, expected: ProviderErrorCode | None
) -> None:
    """Classify every response envelope failure without JSON salvage."""
    if expected is None:
        assert _normalize_fixture(raw).structured_output == {"decision": "STOP"}  # type: ignore[attr-defined]
        return

    with pytest.raises(ProviderError) as error:
        _normalize_fixture(raw)

    assert error.value.code is expected
    assert error.value.metadata is not None
    assert error.value.metadata.response_id == "resp_fixture"


def test_responses_parser_accepts_whitespace_around_structured_json() -> None:
    """Whitespace is valid JSON framing and is retained as one segment."""
    raw = _envelope([_message(_output_text('  \n {"decision":"STOP"} \n  '))])

    assert _normalize_fixture(raw).structured_output == {"decision": "STOP"}  # type: ignore[attr-defined]


def test_responses_parser_reports_schema_mismatch_path() -> None:
    """Valid JSON with missing required fields is a typed schema failure."""
    raw = _envelope([_message(_output_text('{"unexpected":"value"}'))])
    model_request = request().model_copy(
        update={
            "response_schema": {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        }
    )

    with pytest.raises(ProviderError) as error:
        _normalize_fixture(raw, model_request)

    assert error.value.code is ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    assert error.value.metadata is not None
    assert error.value.metadata.schema_error_path == "$.decision"
