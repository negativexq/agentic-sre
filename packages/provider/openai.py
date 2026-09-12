"""Explicit OpenAI Responses API boundary with local credit guards."""

import json
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol, cast

from packages.provider.budget import LiveModelBudget
from packages.provider.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
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
                            "schema": request.response_schema,
                            "strict": True,
                        }
                    },
                    timeout=request.timeout_ms / 1000,
                )
                return self._normalize_response(request, raw, started)
            except ProviderError:
                raise
            except (TimeoutError, ConnectionError) as error:
                if attempt + 1 == attempts:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT
                        if isinstance(error, TimeoutError)
                        else ProviderErrorCode.PROVIDER_UNAVAILABLE,
                        "live provider request failed",
                    ) from error
            except Exception as error:
                status_code = getattr(error, "status_code", None)
                retryable = status_code == 429 or (
                    isinstance(status_code, int) and status_code >= 500
                )
                if retryable and attempt + 1 < attempts:
                    continue
                code = (
                    ProviderErrorCode.RATE_LIMITED
                    if status_code == 429
                    else ProviderErrorCode.PROVIDER_UNAVAILABLE
                )
                raise ProviderError(code, "live provider request failed") from error
        raise AssertionError("provider retry loop must return or raise")

    def _normalize_response(
        self,
        request: ModelRequest,
        raw: Any,
        started: float,
    ) -> ModelResponse:
        """Parse structured JSON and usage without retaining raw provider output."""
        output_text = getattr(raw, "output_text", None)
        if not isinstance(output_text, str):
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE, "provider returned no structured output"
            )
        try:
            structured_output = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "provider structured output was not valid JSON",
            ) from error
        if not isinstance(structured_output, dict):
            raise ProviderError(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "provider structured output was not an object",
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
        )
