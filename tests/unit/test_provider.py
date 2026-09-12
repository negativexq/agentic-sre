"""Offline provider contracts and credit budget tests."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.investigation import InvestigationDecision
from packages.provider import (
    FakeModelProvider,
    LiveModelBudget,
    ModelMessage,
    ModelRequest,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCode,
)
from packages.provider.openai import LiveModelConfig, _compile_strict_schema


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


def test_responses_parser_rejects_multiple_structured_segments() -> None:
    """Multiple output segments fail closed instead of being concatenated."""
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

    assert error.value.code is ProviderErrorCode.INVALID_RESPONSE
