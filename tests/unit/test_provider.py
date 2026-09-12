"""Offline provider contracts and credit budget tests."""

from uuid import uuid4

import pytest

from packages.provider import (
    FakeModelProvider,
    LiveModelBudget,
    ModelMessage,
    ModelRequest,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCode,
)
from packages.provider.openai import LiveModelConfig


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
