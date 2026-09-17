"""Minimal structured-output LLM clients. Live calls are opt-in and budgeted."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

LIVE_ENABLED_ENV = "SRE_LLM_ENABLED"
MODEL_ENV = "SRE_LLM_MODEL"
MAX_CALLS_ENV = "SRE_LLM_MAX_CALLS"
DEFAULT_MODEL = "gpt-5.6-luna"


class LLMError(RuntimeError):
    """The model could not be called or returned unusable output."""


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
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text:
            raise LLMError("model returned no text")
        try:
            value = json.loads(text)
        except ValueError as error:
            raise LLMError("model returned invalid JSON") from error
        if not isinstance(value, dict):
            raise LLMError("model returned a non-object")
        return value


__all__ = [
    "DEFAULT_MODEL",
    "LIVE_ENABLED_ENV",
    "LLMClient",
    "LLMError",
    "OpenAIClient",
    "ScriptedLLM",
]
