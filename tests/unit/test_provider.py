"""Offline provider contracts and credit budget tests."""

import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.investigation import A1CausalDecision, InvestigationDecision
from packages.investigation.registry import live_observability_registry
from packages.provider import (
    FakeModelProvider,
    LiveModelBudget,
    ModelMessage,
    ModelRequest,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCode,
    ProviderFailureMetadata,
    ToolSchemaDescriptor,
)
from packages.provider.openai import (
    A1_DECISION_FUNCTION_NAMES,
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


def a1_function_request() -> ModelRequest:
    """Build a provider request using the structured A1 decision protocol."""
    return request().model_copy(
        update={
            "response_schema_name": "a1_investigation_decision",
            "response_schema": A1CausalDecision.model_json_schema(),
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


def test_shared_live_budget_preserves_benchmark_metadata(tmp_path: Path) -> None:
    """Rich benchmark ledgers retain identity and update their run counters."""
    ledger = tmp_path / "rich-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "cap": 5,
                "calls_used": 0,
                "consumed": 0,
                "remaining": 5,
                "new_smoke_consumed": 0,
                "purpose": "SMOKE",
            }
        ),
        encoding="utf-8",
    )

    LiveModelBudget(5, ledger_path=str(ledger)).consume()

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["calls_used"] == 1
    assert payload["consumed"] == 1
    assert payload["remaining"] == 4
    assert payload["new_smoke_consumed"] == 1
    assert payload["purpose"] == "SMOKE"


def test_paid_budget_requires_a_shared_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paid command setup fails before transport when no persistent ledger is configured."""
    monkeypatch.delenv("SRE_LIVE_MODEL_BUDGET_FILE", raising=False)
    monkeypatch.setenv("SRE_LIVE_MODEL_CALL_BUDGET", "80")

    with pytest.raises(ProviderError) as error:
        LiveModelBudget.from_environment(require_shared_ledger=True)

    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_LEDGER_REQUIRED


def test_ledger_reconciliation_is_guarded_and_never_decrements(tmp_path: Path) -> None:
    """Historical untracked calls can be added only from the expected current value."""
    ledger = tmp_path / "budget.json"
    ledger.write_text('{"calls_used": 43}', encoding="utf-8")

    LiveModelBudget.reconcile_ledger(str(ledger), expected_current=43, corrected_current=45)
    assert ledger.read_text(encoding="utf-8") == '{"calls_used": 45}'

    with pytest.raises(ValueError):
        LiveModelBudget.reconcile_ledger(str(ledger), expected_current=43, corrected_current=46)
    with pytest.raises(ValueError):
        LiveModelBudget.reconcile_ledger(str(ledger), expected_current=45, corrected_current=44)

    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        LiveModelBudget.reconcile_ledger(str(missing), expected_current=0, corrected_current=1)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        LiveModelBudget.reconcile_ledger(str(corrupt), expected_current=43, corrected_current=45)


def test_ledger_delta_must_match_outbound_attempts(tmp_path: Path) -> None:
    """A paid command cannot report success when its shared ledger was bypassed."""
    ledger = tmp_path / "budget.json"
    ledger.write_text('{"calls_used": 43}', encoding="utf-8")
    budget = LiveModelBudget(80, ledger_path=str(ledger))
    before = budget.snapshot()
    budget.consume()
    after = budget.snapshot()
    budget.verify_ledger_delta(before, after, 1)

    with pytest.raises(ProviderError) as error:
        budget.verify_ledger_delta(before, after, 2)
    assert error.value.code is ProviderErrorCode.LIVE_MODEL_BUDGET_LEDGER_MISMATCH


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


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_category"),
    [
        (400, ProviderErrorCode.BAD_REQUEST, "BAD_REQUEST"),
        (401, ProviderErrorCode.AUTHENTICATION_FAILED, "AUTHENTICATION"),
        (403, ProviderErrorCode.PERMISSION_DENIED, "PERMISSION_DENIED"),
        (404, ProviderErrorCode.RESOURCE_NOT_FOUND, "NOT_FOUND"),
        (422, ProviderErrorCode.UNPROCESSABLE_REQUEST, "BAD_REQUEST"),
        (429, ProviderErrorCode.RATE_LIMITED, "RATE_LIMIT"),
        (500, ProviderErrorCode.PROVIDER_UNAVAILABLE, "SERVER_ERROR"),
        (503, ProviderErrorCode.PROVIDER_UNAVAILABLE, "SERVER_ERROR"),
    ],
)
def test_openai_status_failures_preserve_typed_attribution(
    status_code: int,
    expected_code: ProviderErrorCode,
    expected_category: str,
) -> None:
    """Map representative OpenAI API statuses without making a network call."""

    class SDKError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("request rejected")
            self.status_code = status_code
            self.body = {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_function_parameters",
                    "param": "tools[0].parameters",
                }
            }
            self.request_id = "req_test_123"

    class Transport:
        def create(self, **_kwargs: object) -> object:
            raise SDKError()

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is expected_code
    failure = error.value.failure_metadata
    assert failure is not None
    assert failure.category == expected_category
    assert failure.http_status_code == status_code
    assert failure.api_error_type == "invalid_request_error"
    assert failure.api_error_code == "invalid_function_parameters"
    assert failure.api_error_param == "tools[0].parameters"
    assert failure.request_id == "req_test_123"


@pytest.mark.parametrize(
    ("exception_name", "expected_code", "expected_category"),
    [
        ("APIConnectionError", ProviderErrorCode.PROVIDER_UNAVAILABLE, "CONNECTION"),
        ("APITimeoutError", ProviderErrorCode.PROVIDER_TIMEOUT, "TIMEOUT"),
    ],
)
def test_openai_connection_and_timeout_failures_are_distinct(
    exception_name: str,
    expected_code: ProviderErrorCode,
    expected_category: str,
) -> None:
    """Classify SDK-like transport exceptions by failure mode."""
    error_type = type(exception_name, (RuntimeError,), {})

    class Transport:
        def create(self, **_kwargs: object) -> object:
            raise error_type("transport failed")

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    assert error.value.code is expected_code
    assert error.value.failure_metadata is not None
    assert error.value.failure_metadata.category == expected_category


def test_openai_failure_metadata_redacts_common_credentials() -> None:
    """Persisted diagnostics do not retain bearer tokens or OpenAI keys."""

    class SDKError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("Authorization: Bearer abc sk-testsecret")
            self.message = "Authorization: Bearer abc sk-testsecret"
            self.status_code = 400

    class Transport:
        def create(self, **_kwargs: object) -> object:
            raise SDKError()

    provider = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    )

    with pytest.raises(ProviderError) as error:
        provider.complete(request())

    failure = error.value.failure_metadata
    assert failure is not None
    assert "abc" not in (failure.message_summary or "")
    assert "sk-testsecret" not in (failure.message_summary or "")
    assert "REDACTED" in (failure.message_summary or "")
    assert len(failure.message_summary or "") <= 500


def test_provider_failure_metadata_contract_is_strict_and_bounded() -> None:
    """The failure payload has no unbounded or undeclared fields."""
    metadata = ProviderFailureMetadata(
        category="BAD_REQUEST",
        http_status_code=400,
        message_summary="safe",
    )
    assert metadata.provider == "openai"
    with pytest.raises(ValueError):
        ProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "raw_response_body": "secret"}
        )


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


def test_a1_transport_exposes_structured_terminal_functions() -> None:
    """The A1 transport carries typed causal and STOP payloads."""
    request = a1_function_request()
    schemas = _decision_function_schemas(request.response_schema, a1_protocol=True)
    assert tuple(schemas) == A1_DECISION_FUNCTION_NAMES
    assert set(schemas["submit_causal_hypothesis"]["properties"]) == {
        "reason",
        "symptom_component",
        "causal_component",
        "causal_resource",
        "mechanism",
        "structured_trigger",
        "causal_summary",
        "evidence_ids",
    }
    normalized = _normalize_fixture(
        _envelope(
            [
                _function_call(
                    json.dumps(
                        {
                            "reason": "insufficient evidence",
                            "stop_reason": "insufficient_evidence",
                            "considered_components": ["order-service"],
                            "considered_resources": [],
                            "missing_evidence_categories": ["TRACES"],
                        }
                    ),
                    name="stop_causal_investigation",
                )
            ]
        ),
        request,
    )
    assert normalized.structured_output["stop"]["considered_components"] == ["order-service"]  # type: ignore[attr-defined]

    captured: dict[str, object] = {}

    class Transport:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return _envelope(
                [
                    _function_call(
                        json.dumps(
                            {
                                "reason": "insufficient evidence",
                                "stop_reason": "insufficient_evidence",
                                "considered_components": [],
                                "considered_resources": [],
                                "missing_evidence_categories": [],
                            }
                        ),
                        name="stop_causal_investigation",
                    )
                ]
            )

    response = OpenAIProvider(
        budget=LiveModelBudget(1),
        config=LiveModelConfig(enabled=True),
        transport=Transport(),
        max_retry=0,
    ).complete(a1_function_request())
    assert response.structured_output["stop"]["stop_reason"] == "insufficient_evidence"
    tools = captured["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == list(A1_DECISION_FUNCTION_NAMES)


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
