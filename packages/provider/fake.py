"""Deterministic provider used by all normal tests and local agent checks."""

from collections.abc import Callable, Iterable
from time import monotonic
from typing import Any

from packages.provider.budget import LiveModelBudget
from packages.provider.contracts import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderAccountingSnapshot,
)

FakeResponse = dict[str, Any] | Callable[[ModelRequest], dict[str, Any]]


class FakeModelProvider:
    """Return scripted structured outputs without making network requests."""

    provider_name = "fake"

    def __init__(
        self,
        responses: Iterable[FakeResponse],
        *,
        model: str = "fake-model",
        input_tokens: int = 0,
        output_tokens: int = 0,
        budget: LiveModelBudget | None = None,
    ) -> None:
        self._responses = list(responses)
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._budget = budget
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the next scripted output and record the validated request."""
        self.requests.append(request)
        if self._budget is not None:
            self._budget.consume()
        if not self._responses:
            raise RuntimeError("fake model response script exhausted")
        started = monotonic()
        scripted = self._responses.pop(0)
        output = scripted(request) if callable(scripted) else scripted
        return ModelResponse(
            request_id=request.request_id,
            structured_output=output,
            provider=self.provider_name,
            model=self._model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            latency_ms=int((monotonic() - started) * 1000),
            finish_reason="scripted",
        )

    def accounting_snapshot(self) -> ProviderAccountingSnapshot:
        """Expose deterministic invocation counts without any network accounting."""
        return ProviderAccountingSnapshot(provider_invocations=len(self.requests))


def is_fake_provider(provider: ModelProvider) -> bool:
    """Identify the offline provider without importing any agent framework."""
    return isinstance(provider, FakeModelProvider)
