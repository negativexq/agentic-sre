"""Minimal structured-output LLM clients. Live calls are opt-in and budgeted."""

from __future__ import annotations

import json
import os
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
            raise LLMError(f"model call failed: {type(error).__name__}") from error
        status = getattr(response, "status", None)
        if status not in (None, "completed"):
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or status
            raise LLMOutputError(f"model response {status}: {reason}")
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text:
            raise LLMOutputError("model returned no text (possibly a refusal)")
        return parse_first_object(text)


__all__ = [
    "DEFAULT_MODEL",
    "LIVE_ENABLED_ENV",
    "LLMClient",
    "LLMError",
    "LLMOutputError",
    "OpenAIClient",
    "parse_first_object",
    "ScriptedLLM",
]
