"""LLM investigator behaviour with scripted models; no network."""

from __future__ import annotations

from typing import Any

import pytest
from rca_builders import config_change_source, ref

from packages.rca.agent import DECISION_SCHEMA, LLMInvestigator
from packages.rca.engine import diagnose
from packages.rca.llm import LLMError, LLMOutputError, OpenAIClient, ScriptedLLM
from packages.rca.model import Confidence


def _reply(action: str, target: str, tool: str | None = None) -> dict[str, Any]:
    return {"action": action, "tool": tool, "target": target, "rationale": "because"}


def test_investigator_inspects_then_concludes_and_engine_verifies() -> None:
    llm = ScriptedLLM([_reply("inspect", "C1", "history"), _reply("conclude", "C1")])
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert diagnosis.mode == "llm" and diagnosis.model_calls == 2
    assert "C1 shop/ConfigMap/checkout-flags" in llm.prompts[0]
    assert "CONFIG_CHANGE" in llm.prompts[1] and "checkoutFailure" in llm.prompts[1]
    actions = [(s.actor, s.action) for s in diagnosis.steps]
    assert ("llm", "history") in actions and ("llm", "conclude") in actions


def test_model_choice_is_rechecked_not_trusted() -> None:
    llm = ScriptedLLM([_reply("conclude", "C2")])
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert any(step.action == "kept" for step in diagnosis.steps)


def test_model_calls_are_counted_per_incident() -> None:
    llm = ScriptedLLM(
        [_reply("conclude", "C1"), _reply("inspect", "C1", "events"), _reply("conclude", "C1")]
    )
    investigator = LLMInvestigator(llm)
    first = diagnose(config_change_source(), investigator=investigator)
    second = diagnose(config_change_source(), investigator=investigator)
    assert (first.model_calls, second.model_calls) == (1, 2)


def test_unusable_output_is_retried_once() -> None:
    def broken(_: str) -> dict[str, Any]:
        raise LLMOutputError("model returned invalid JSON")

    llm = ScriptedLLM([broken, _reply("conclude", "C1")])
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert diagnosis.model_calls == 2
    assert [s.action for s in diagnosis.steps if s.actor == "llm"] == ["retry", "conclude"]

    twice = ScriptedLLM([broken, broken])
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(twice))
    assert [s.action for s in diagnosis.steps if s.actor == "llm"] == ["retry", "error"]
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")


def test_openai_client_reports_incomplete_and_invalid_output() -> None:
    class Incomplete:
        status = "incomplete"
        incomplete_details = type("D", (), {"reason": "max_output_tokens"})()
        output_text = '{"action": "concl'

    class Truncated:
        status = "completed"
        output_text = '{"action": "concl'

    def sdk_returning(result: Any) -> Any:
        class Responses:
            def create(self, **_: Any) -> Any:
                return result

        return type("SDK", (), {"responses": Responses()})()

    for result, message in ((Incomplete(), "max_output_tokens"), (Truncated(), "invalid JSON")):
        client = OpenAIClient(enabled=True, max_calls=1, client=sdk_returning(result))
        with pytest.raises(LLMOutputError, match=message):
            client.complete_json(system="s", user="u", schema=DECISION_SCHEMA, name="rca")


def test_invalid_replies_fall_back_to_the_engine_ranking() -> None:
    llm = ScriptedLLM([_reply("conclude", "C99"), _reply("inspect", "C1", "rm -rf")])
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.model_calls == 2
    assert [s.action for s in diagnosis.steps].count("rejected") == 2


def test_non_candidate_conclusion_is_rejected_even_if_the_object_exists() -> None:
    llm = ScriptedLLM(
        [
            _reply("conclude", "shop/Service/checkout"),
            _reply("conclude", "C1"),
        ]
    )
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(llm))
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.model_calls == 2


def test_last_step_forces_a_conclusion() -> None:
    llm = ScriptedLLM([_reply("inspect", "C1", "events"), _reply("inspect", "C1", "events")])
    investigator = LLMInvestigator(llm, max_steps=2)
    diagnosis = diagnose(config_change_source(), investigator=investigator)
    assert "last step" in llm.prompts[1]
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")


def test_model_failure_is_recorded_and_engine_answer_is_kept() -> None:
    diagnosis = diagnose(config_change_source(), investigator=LLMInvestigator(ScriptedLLM([])))
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert any(s.action == "error" for s in diagnosis.steps)


def test_openai_client_refuses_without_opt_in_and_budget() -> None:
    with pytest.raises(LLMError, match="disabled"):
        OpenAIClient(enabled=False, max_calls=5).complete_json(
            system="s", user="u", schema=DECISION_SCHEMA, name="n"
        )
    with pytest.raises(LLMError, match="budget"):
        OpenAIClient(enabled=True, max_calls=0).complete_json(
            system="s", user="u", schema=DECISION_SCHEMA, name="n"
        )


def test_openai_client_parses_strict_json_output() -> None:
    class Responses:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def create(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs

            class Result:
                output_text = (
                    '{"action": "conclude", "tool": null, "target": "C1", "rationale": "x"}'
                )

            return Result()

    class SDK:
        responses = Responses()

    client = OpenAIClient(enabled=True, max_calls=1, model="m", client=SDK())
    value = client.complete_json(system="s", user="u", schema=DECISION_SCHEMA, name="rca")
    assert value["target"] == "C1"
    assert SDK.responses.kwargs["text"]["format"]["strict"] is True
    assert client.calls == 1
    with pytest.raises(LLMError, match="budget"):
        client.complete_json(system="s", user="u", schema=DECISION_SCHEMA, name="rca")
