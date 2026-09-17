"""Offline tests for the project-wide paid-model identity boundary."""

from __future__ import annotations

import pytest

from packages.model_policy import (
    APPROVED_MODEL,
    ModelPolicyError,
    validate_agent_config,
    validate_agent_environment,
    validate_judge_environment,
)
from packages.provider import LiveModelBudget, OpenAIProvider, ProviderError, live_model_config
from packages.provider.openai import LiveModelConfig


def test_judge_requires_explicit_model() -> None:
    with pytest.raises(ModelPolicyError, match="JUDGE_MODEL") as error:
        validate_judge_environment(_judge_env(JUDGE_MODEL=""), require_credentials=False)
    assert error.value.code == "JUDGE_MODEL_NOT_EXPLICITLY_APPROVED"


def test_judge_rejects_upstream_default_model() -> None:
    with pytest.raises(ModelPolicyError) as error:
        validate_judge_environment(_judge_env(JUDGE_MODEL="gpt-4-turbo"), require_credentials=False)
    assert error.value.code == "JUDGE_MODEL_NOT_EXPLICITLY_APPROVED"


def test_judge_luna_is_allowed_without_network() -> None:
    identity = validate_judge_environment(_judge_env(), require_credentials=False)
    assert identity.as_dict() == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "purpose": "JUDGE",
        "base_url_host": "api.openai.com",
    }


def test_judge_requires_credentials_for_paid_launch() -> None:
    with pytest.raises(ModelPolicyError) as error:
        validate_judge_environment(_judge_env(JUDGE_API_KEY=""))
    assert error.value.code == "JUDGE_CREDENTIALS_MISSING"


def test_judge_rejects_unestablished_provider() -> None:
    with pytest.raises(ModelPolicyError) as error:
        validate_judge_environment(_judge_env(JUDGE_PROVIDER="other"), require_credentials=False)
    assert error.value.code == "JUDGE_PROVIDER_NOT_APPROVED"


def test_judge_does_not_accept_credentials_in_base_url() -> None:
    with pytest.raises(ModelPolicyError) as error:
        validate_judge_environment(
            _judge_env(JUDGE_BASE_URL="https://secret:token@example.invalid/v1"),
            require_credentials=False,
        )
    assert error.value.code == "JUDGE_PROVIDER_NOT_ESTABLISHED"


def test_agent_requires_exact_model_and_reasoning() -> None:
    assert validate_agent_config("gpt-5.6-luna", "none").purpose == "AGENT"
    with pytest.raises(ModelPolicyError) as error:
        validate_agent_config("gpt-4-turbo", "none")
    assert error.value.code == "BENCHMARK_MODEL_POLICY_VIOLATION"
    with pytest.raises(ModelPolicyError) as error:
        validate_agent_config("gpt-5.6-luna", "low")
    assert error.value.code == "BENCHMARK_MODEL_POLICY_VIOLATION"


def test_agent_environment_has_no_implicit_defaults() -> None:
    with pytest.raises(ModelPolicyError) as error:
        validate_agent_environment({})
    assert error.value.code == "BENCHMARK_MODEL_POLICY_VIOLATION"

    identity = validate_agent_environment(
        {"SRE_MODEL": "gpt-5.6-luna", "SRE_REASONING_EFFORT": "none"}
    )
    assert identity.as_dict() == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "purpose": "AGENT",
        "reasoning": "none",
    }


def test_live_model_config_does_not_supply_a_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SRE_MODEL", raising=False)
    monkeypatch.delenv("SRE_REASONING_EFFORT", raising=False)
    config = live_model_config()
    assert config.model == ""
    assert config.reasoning_effort == ""


def test_live_provider_blocks_non_luna_config_before_transport() -> None:
    class Transport:
        def create(self, **_: object) -> object:
            raise AssertionError("transport must not be reached")

    with pytest.raises(ProviderError) as error:
        OpenAIProvider(
            budget=LiveModelBudget(1),
            config=LiveModelConfig(enabled=True, model="gpt-4-turbo", reasoning_effort="none"),
            transport=Transport(),
        )
    assert error.value.code.value == "BENCHMARK_MODEL_POLICY_VIOLATION"


def _judge_env(**overrides: str) -> dict[str, str]:
    value = {
        "JUDGE_MODEL": APPROVED_MODEL,
        "JUDGE_PROVIDER": "openai",
        "JUDGE_BASE_URL": "https://api.openai.com/v1",
        "JUDGE_API_KEY": "offline-test-placeholder",
    }
    value.update(overrides)
    return value
