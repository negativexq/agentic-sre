"""Offline tests for the project-wide paid-model identity boundary."""

from __future__ import annotations

import json
from pathlib import Path

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


def _judge_env(**overrides: str) -> dict[str, str]:
    value = {
        "JUDGE_MODEL": APPROVED_MODEL,
        "JUDGE_PROVIDER": "openai",
        "JUDGE_BASE_URL": "https://api.openai.com/v1",
        "JUDGE_API_KEY": "offline-test-placeholder",
    }
    value.update(overrides)
    return value


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


def test_upstream_default_is_never_reached_by_wrapper_preflight(tmp_path: Path) -> None:
    """The missing-env case fails before the pinned evaluator can import its client."""
    from packages.evals.itbench.judge_policy import build_judge_plan, preflight_judge

    ledger = tmp_path / "judge.json"
    ledger.write_text(json.dumps({"cap": 1, "consumed": 0, "remaining": 1}), encoding="utf-8")
    plan = build_judge_plan(expected_calls=1, max_calls=1, ledger_path=ledger)
    with pytest.raises(ModelPolicyError) as error:
        preflight_judge(plan=plan, environ={}, require_credentials=False)
    assert error.value.code == "JUDGE_MODEL_NOT_EXPLICITLY_APPROVED"


def test_judge_plan_requires_explicit_finite_cap(tmp_path: Path) -> None:
    from packages.evals.itbench.judge_policy import build_judge_plan, preflight_judge

    ledger = tmp_path / "judge.json"
    ledger.write_text(json.dumps({"cap": 2, "consumed": 0, "remaining": 2}), encoding="utf-8")
    with pytest.raises(ModelPolicyError) as error:
        build_judge_plan(expected_calls=2, max_calls=1, ledger_path=ledger)
    assert error.value.code == "JUDGE_CALL_CAP_EXCEEDED"

    plan = build_judge_plan(expected_calls=1, max_calls=1, ledger_path=ledger)
    identity, before = preflight_judge(plan=plan, environ=_judge_env(), require_credentials=False)
    assert identity.model == "gpt-5.6-luna"
    assert before["consumed"] == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["consumed"] == 0
