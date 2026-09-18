"""Minimal structured-output LLM clients. Live calls are opt-in and budgeted."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

LIVE_ENABLED_ENV = "SRE_LLM_ENABLED"
MODEL_ENV = "SRE_LLM_MODEL"
MAX_CALLS_ENV = "SRE_LLM_MAX_CALLS"
DEFAULT_MODEL = "gpt-5.6-luna"


class LLMError(RuntimeError):
    """The model could not be called or returned unusable output."""


class LLMOutputError(LLMError):
    """The call succeeded but the output was unusable; retrying may help."""


@dataclass(frozen=True)
class ProviderErrorDetails:
    """Safe, bounded diagnostics for one provider request failure."""

    category: str
    exception_type: str
    status_code: int | None = None
    code: str | None = None
    parameter: str | None = None
    request_id: str | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "exception_type": self.exception_type,
            "status_code": self.status_code,
            "code": self.code,
            "parameter": self.parameter,
            "request_id": self.request_id,
            "message": self.message,
        }


class ProviderRequestError(LLMError):
    """The provider rejected the constructed request before model output."""

    def __init__(self, details: ProviderErrorDetails) -> None:
        self.details = details
        super().__init__(json.dumps(details.as_dict(), sort_keys=True))


class ProviderTransportError(LLMError):
    """The provider request failed without a request-level response."""

    def __init__(self, details: ProviderErrorDetails) -> None:
        self.details = details
        super().__init__(json.dumps(details.as_dict(), sort_keys=True))


class LLMClient(Protocol):
    model: str
    calls: int

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], name: str
    ) -> dict[str, Any]: ...


Reply = dict[str, Any] | Callable[[str], dict[str, Any]]


@dataclass
class ScriptedLLM:
    """Replays fixed replies; a callable reply receives the prompt."""

    replies: list[Reply]
    model: str = "scripted"
    calls: int = 0
    prompts: list[str] = field(default_factory=list)

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], name: str
    ) -> dict[str, Any]:
        if self.calls >= len(self.replies):
            raise LLMError("scripted replies exhausted")
        reply = self.replies[self.calls]
        self.calls += 1
        self.prompts.append(user)
        return reply(user) if callable(reply) else dict(reply)


def parse_first_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, accepting a reply that repeats objects back to back."""
    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        value, end = decoder.raw_decode(stripped)
    except ValueError as error:
        raise LLMOutputError(
            f"model returned invalid JSON ({len(text)} chars, ends {text[-20:]!r})"
        ) from error
    if not isinstance(value, dict):
        raise LLMOutputError("model returned a non-object")
    rest = stripped[end:].strip()
    while rest:
        # Extra objects are tolerated only if they are well-formed JSON objects.
        try:
            extra, end = decoder.raw_decode(rest)
        except ValueError as error:
            raise LLMOutputError(
                f"model returned trailing text after JSON: {rest[:20]!r}"
            ) from error
        if not isinstance(extra, dict):
            raise LLMOutputError("model returned trailing non-object JSON")
        rest = rest[end:].strip()
    return value


class OpenAIClient:
    """OpenAI Responses API with strict JSON schema output.

    Refuses to call the API unless ``SRE_LLM_ENABLED=true`` and a positive call
    budget is set, so tests and offline runs can never spend money by accident.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_calls: int | None = None,
        enabled: bool | None = None,
        client: Any | None = None,
    ) -> None:
        self.model: str = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.max_calls = (
            max_calls if max_calls is not None else int(os.environ.get(MAX_CALLS_ENV, "0"))
        )
        self.enabled = (
            enabled
            if enabled is not None
            else os.environ.get(LIVE_ENABLED_ENV, "").casefold() == "true"
        )
        self.calls = 0
        self.successful_responses = 0
        self.last_response_status: str | None = None
        self.last_request_id: str | None = None
        self._client = client
        # Shared across concurrent callers (e.g. the control plane diagnosing
        # more than one incident at once) so the budget is process-wide.
        self._lock = threading.Lock()

    def readiness_problem(self) -> str | None:
        """Why live calls would fail before the first request, or None when ready."""
        if not self.enabled:
            return f"live model calls are disabled; set {LIVE_ENABLED_ENV}=true"
        if self.max_calls <= 0:
            return (
                f"no model call budget; set {MAX_CALLS_ENV} to the total calls allowed for this run"
            )
        if self._client is None and not os.environ.get("OPENAI_API_KEY"):
            return "OPENAI_API_KEY is not set"
        return None

    def _sdk(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], name: str
    ) -> dict[str, Any]:
        if not self.enabled:
            raise LLMError(f"live model calls are disabled; set {LIVE_ENABLED_ENV}=true")
        with self._lock:
            if self.calls >= self.max_calls:
                raise LLMError(f"model call budget exhausted ({self.max_calls})")
            self.calls += 1
        try:
            response = self._sdk().responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
        except Exception as error:  # the SDK raises many transport-specific types
            details = _provider_error_details(error)
            if details.status_code is not None and 400 <= details.status_code < 500:
                raise ProviderRequestError(details) from error
            raise ProviderTransportError(details) from error
        status = getattr(response, "status", None)
        self.last_response_status = str(status) if status is not None else None
        self.last_request_id = _request_id(response)
        if status not in (None, "completed"):
            incomplete_details = getattr(response, "incomplete_details", None)
            reason = getattr(incomplete_details, "reason", None) or status
            raise LLMOutputError(f"model response {status}: {reason}")
        self.successful_responses += 1
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text:
            raise LLMOutputError("model returned no text (possibly a refusal)")
        return parse_first_object(text)


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)bearer\s+[^\s,;]+"),
    re.compile(r"(?i)(?:api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]+"),
)


def _sanitized_message(error: BaseException) -> str:
    """Keep provider diagnostics useful without exposing credentials or payloads."""
    raw = getattr(error, "message", None) or str(error)
    message = " ".join(str(raw).split())[:1000]
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    return message


def _request_id(response: Any) -> str | None:
    value = getattr(response, "request_id", None) or getattr(response, "_request_id", None)
    return str(value)[:200] if value else None


def _provider_error_details(error: BaseException) -> ProviderErrorDetails:
    status_raw = getattr(error, "status_code", None)
    status_code = status_raw if isinstance(status_raw, int) else None
    code_raw = getattr(error, "code", None)
    parameter_raw = getattr(error, "param", None) or getattr(error, "parameter", None)
    request_raw = getattr(error, "request_id", None) or getattr(error, "_request_id", None)
    category = (
        "PROVIDER_REQUEST_ERROR"
        if status_code is not None and 400 <= status_code < 500
        else "PROVIDER_TRANSPORT_ERROR"
    )
    return ProviderErrorDetails(
        category=category,
        exception_type=type(error).__name__,
        status_code=status_code,
        code=str(code_raw)[:200] if code_raw is not None else None,
        parameter=str(parameter_raw)[:200] if parameter_raw is not None else None,
        request_id=str(request_raw)[:200] if request_raw is not None else None,
        message=_sanitized_message(error),
    )


__all__ = [
    "DEFAULT_MODEL",
    "LIVE_ENABLED_ENV",
    "LLMClient",
    "LLMError",
    "LLMOutputError",
    "OpenAIClient",
    "ProviderErrorDetails",
    "ProviderRequestError",
    "ProviderTransportError",
    "parse_first_object",
    "ScriptedLLM",
]
