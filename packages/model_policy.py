"""Fail-closed model identity policy for every paid project execution.

This module deliberately contains no provider client code.  It is shared by
the agent and evaluator launch boundaries so neither path can inherit a
third-party library's model default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

APPROVED_PROVIDER = "openai"
APPROVED_MODEL = "gpt-5.6-luna"
APPROVED_REASONING = "none"


class ModelPolicyError(RuntimeError):
    """A paid execution was blocked before a provider client was called."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelExecutionIdentity:
    """Safe, non-secret identity persisted with a paid execution artifact."""

    provider: str
    model: str
    purpose: str
    reasoning: str | None = None
    base_url_host: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
        }
        if self.reasoning is not None:
            value["reasoning"] = self.reasoning
        if self.base_url_host is not None:
            value["base_url_host"] = self.base_url_host
        return value


def validate_agent_config(model: str, reasoning: str) -> ModelExecutionIdentity:
    """Validate explicit benchmark-agent model settings."""
    if model != APPROVED_MODEL or reasoning != APPROVED_REASONING:
        raise ModelPolicyError(
            "BENCHMARK_MODEL_POLICY_VIOLATION",
            "agent execution requires gpt-5.6-luna with reasoning=none",
        )
    return ModelExecutionIdentity(
        provider=APPROVED_PROVIDER,
        model=APPROVED_MODEL,
        reasoning=APPROVED_REASONING,
        purpose="AGENT",
    )


def validate_agent_environment(
    environ: Mapping[str, str] | None = None,
) -> ModelExecutionIdentity:
    """Require explicit agent environment values; never use an implicit default."""
    env = os.environ if environ is None else environ
    model = env.get("SRE_MODEL")
    reasoning = env.get("SRE_REASONING_EFFORT")
    if model is None or reasoning is None:
        raise ModelPolicyError(
            "BENCHMARK_MODEL_POLICY_VIOLATION",
            "SRE_MODEL and SRE_REASONING_EFFORT must be explicitly set",
        )
    return validate_agent_config(model, reasoning)


def _safe_base_url_host(environ: Mapping[str, str]) -> str:
    raw = environ.get("JUDGE_BASE_URL") or "https://api.openai.com/v1"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelPolicyError(
            "JUDGE_PROVIDER_NOT_ESTABLISHED",
            "JUDGE_BASE_URL must be an HTTP(S) URL with a hostname",
        )
    if parsed.username or parsed.password:
        raise ModelPolicyError(
            "JUDGE_PROVIDER_NOT_ESTABLISHED",
            "JUDGE_BASE_URL must not contain credentials",
        )
    return parsed.hostname


def validate_judge_environment(
    environ: Mapping[str, str] | None = None,
    *,
    require_credentials: bool = True,
) -> ModelExecutionIdentity:
    """Validate the evaluator identity before constructing an LLM client.

    ``JUDGE_MODEL`` is intentionally required even though the pinned upstream
    evaluator defaults to ``gpt-4-turbo``.  The optional credential switch is
    only for offline preflight tests; a real launch must require credentials.
    """
    env = os.environ if environ is None else environ
    provider = env.get("JUDGE_PROVIDER", APPROVED_PROVIDER)
    if provider != APPROVED_PROVIDER:
        raise ModelPolicyError(
            "JUDGE_PROVIDER_NOT_APPROVED",
            "judge provider must be explicitly compatible with OpenAI",
        )
    if env.get("JUDGE_MODEL") != APPROVED_MODEL:
        raise ModelPolicyError(
            "JUDGE_MODEL_NOT_EXPLICITLY_APPROVED",
            "JUDGE_MODEL must be explicitly set to gpt-5.6-luna",
        )
    host = _safe_base_url_host(env)
    if require_credentials and not env.get("JUDGE_API_KEY"):
        raise ModelPolicyError(
            "JUDGE_CREDENTIALS_MISSING",
            "JUDGE_API_KEY is required before a paid judge launch",
        )
    return ModelExecutionIdentity(
        provider=APPROVED_PROVIDER,
        model=APPROVED_MODEL,
        purpose="JUDGE",
        base_url_host=host,
    )


__all__ = [
    "APPROVED_MODEL",
    "APPROVED_PROVIDER",
    "APPROVED_REASONING",
    "ModelExecutionIdentity",
    "ModelPolicyError",
    "validate_agent_config",
    "validate_agent_environment",
    "validate_judge_environment",
]
