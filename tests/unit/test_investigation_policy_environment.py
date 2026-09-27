"""M20.2: investigation policy selection is separate from model availability."""

from __future__ import annotations

import pytest

from apps.control_plane.diagnosis import (
    InvestigationConfigurationError,
    investigation_policy_from_environment,
)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        pytest.param({"SRE_INVESTIGATION_ENABLED": "false"}, None, id="off"),
        pytest.param(
            {
                "SRE_INVESTIGATION_ENABLED": "false",
                "SRE_INVESTIGATION_POLICY": "llm",
                "SRE_LLM_ENABLED": "false",
            },
            None,
            id="off-ignores-policy",
        ),
        pytest.param({}, "deterministic_intent", id="unset-defaults-on-intent"),
        pytest.param(
            {"SRE_LLM_ENABLED": "true"}, "deterministic_intent", id="llm-flag-does-not-pick-llm"
        ),
        pytest.param(
            {
                "SRE_INVESTIGATION_ENABLED": "TRUE",
                "SRE_INVESTIGATION_POLICY": "deterministic_intent",
            },
            "deterministic_intent",
            id="intent",
        ),
        pytest.param(
            {"SRE_INVESTIGATION_POLICY": "deterministic_observation", "SRE_LLM_ENABLED": "true"},
            "deterministic_observation",
            id="observation",
        ),
        pytest.param(
            {"SRE_INVESTIGATION_POLICY": "llm", "SRE_LLM_ENABLED": "true"}, "llm", id="llm"
        ),
    ],
)
def test_the_environment_selects_exactly_one_policy(
    environ: dict[str, str], expected: str | None
) -> None:
    assert investigation_policy_from_environment(environ) == expected


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        pytest.param(
            {"SRE_INVESTIGATION_POLICY": "llm"}, "requires SRE_LLM_ENABLED", id="llm-unavailable"
        ),
        pytest.param(
            {"SRE_INVESTIGATION_POLICY": "llm", "SRE_LLM_ENABLED": "false"},
            "requires SRE_LLM_ENABLED",
            id="llm-disabled",
        ),
        pytest.param(
            {"SRE_INVESTIGATION_POLICY": "smart", "SRE_LLM_ENABLED": "true"},
            "SRE_INVESTIGATION_POLICY",
            id="unknown-policy",
        ),
        pytest.param(
            {"SRE_INVESTIGATION_ENABLED": "yes"}, "SRE_INVESTIGATION_ENABLED", id="bad-flag"
        ),
    ],
)
def test_an_invalid_environment_is_a_configuration_error(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(InvestigationConfigurationError, match=message):
        investigation_policy_from_environment(environ)
