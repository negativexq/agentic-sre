from __future__ import annotations

import json

import pytest

from packages.rca.investigation.intents import (
    IntentMenuItem,
    InvestigationIntentKind,
    InvestigationPhase,
)
from packages.rca.investigation.policy import (
    INTENT_SELECTION_SCHEMA,
    LLMIntentPolicy,
)
from packages.rca.llm import LLMError, ScriptedLLM
from packages.rca.model import GapDimension


def _menu(*intent_ids: str) -> tuple[IntentMenuItem, ...]:
    return tuple(
        IntentMenuItem(
            intent_id=intent_id,
            intent=InvestigationIntentKind.ACTOR_STATE_INSPECTION,
            phase=InvestigationPhase.SOURCE_DISCOVERY,
            dimensions=(GapDimension.ENTITY_STATE,),
            capabilities=("events",),
            physical_candidate_count=2,
            decision_blocker_match=3,
            leading_hypothesis_relevance=0,
            unresolved_hypothesis_relevance=0,
            eligibility_relevance=0,
            semantic_priority=2,
        )
        for intent_id in intent_ids
    )


def test_intent_schema_contains_only_intent_id() -> None:
    assert set(INTENT_SELECTION_SCHEMA["properties"]) == {"intent_id"}
    assert INTENT_SELECTION_SCHEMA["required"] == ["intent_id"]
    assert INTENT_SELECTION_SCHEMA["additionalProperties"] is False


def test_valid_intent_selection_uses_only_semantic_menu() -> None:
    client = ScriptedLLM(replies=[{"intent_id": "intent:actor"}])
    policy = LLMIntentPolicy(client)
    menu = _menu("intent:actor", "intent:source")

    assert policy.choose_intent(menu) == "intent:actor"
    prompt = client.prompts[0]
    payload = json.loads(prompt)
    assert payload["menu"][0]["intent_id"] == "intent:actor"
    assert "shop/Deployment/secret" not in prompt
    assert "candidate_id" not in prompt
    assert "target" not in prompt
    assert "query" not in prompt
    assert "OPENAI_API_KEY" not in prompt


def test_history_is_bounded_and_semantic() -> None:
    client = ScriptedLLM(replies=[{"intent_id": "intent:actor"}])
    policy = LLMIntentPolicy(client)
    history = tuple(
        {
            "intent_kind": "ACTOR_STATE_INSPECTION",
            "capability": "events",
            "outcome": "NO_DATA",
            "returned_refs": 0,
            "new_refs": 0,
            "findings": 0,
            "decision_state_changed": False,
            "target": "must-not-be-forwarded",
        }
        for _ in range(8)
    )

    policy.choose_intent(_menu("intent:actor"), history)
    payload = json.loads(client.prompts[0])
    assert len(payload["previous_observations"]) == 6
    assert "target" not in payload["previous_observations"][0]


def test_malformed_output_gets_one_bounded_retry() -> None:
    client = ScriptedLLM(
        replies=[
            {"target": "shop/Deployment/cart"},
            {"intent_id": "intent:actor"},
        ]
    )
    policy = LLMIntentPolicy(client)

    assert policy.choose_intent(_menu("intent:actor")) == "intent:actor"
    assert client.calls == 2


def test_provider_failure_is_not_converted_into_another_planner() -> None:
    class FailingClient(ScriptedLLM):
        def complete_json(self, **_: object) -> dict[str, object]:
            self.calls += 1
            raise LLMError("offline")

    policy = LLMIntentPolicy(FailingClient(replies=[]))
    with pytest.raises(LLMError):
        policy.choose_intent(_menu("intent:actor", "intent:source"))


def test_live_client_contract_is_fixed_to_luna_without_exposing_credentials() -> None:
    from packages.rca.llm import OpenAIClient

    client = OpenAIClient(enabled=False, max_calls=1)
    assert client.model == "gpt-5.6-luna"
    assert client.reasoning_effort == "none"
