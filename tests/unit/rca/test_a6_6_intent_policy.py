from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.environment import initial_view
from packages.rca.investigation.graph import build_investigation_state, investigate_diagnosis
from packages.rca.investigation.intents import (
    DeterministicIntentPolicy,
    IntentMenuItem,
    InvestigationIntentKind,
    InvestigationPhase,
)
from packages.rca.investigation.policy import (
    INTENT_SELECTION_SCHEMA,
    LLMIntentPolicy,
    ScriptedInvestigationPolicy,
)
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tools import make_observation
from packages.rca.llm import (
    LLMError,
    OpenAIClient,
    ProviderTransportError,
    ScriptedLLM,
)
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InformationGapOrigin,
    InvestigationAction,
    InvestigationObservation,
    InvestigationQuery,
)
from packages.rca.source import InMemorySource


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
    client = OpenAIClient(enabled=False, max_calls=1)
    assert client.model == "gpt-5.6-luna"
    assert client.reasoning_effort == "none"


def test_intent_policy_accepts_configuration_driven_model_identity() -> None:
    client = OpenAIClient(model="gpt-6-luna", enabled=False, max_calls=1)

    policy = LLMIntentPolicy(client)

    assert policy.client.model == "gpt-6-luna"
    assert client.calls == 0


def test_openai_sdk_is_bounded_without_touching_injected_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    client = OpenAIClient(enabled=True, max_calls=1)
    client._sdk()
    assert captured == {"timeout": 20.0, "max_retries": 0}


def test_transport_failure_is_not_retried() -> None:
    class Responses:
        calls = 0

        def create(self, **_: object) -> object:
            self.calls += 1
            raise TimeoutError("provider timeout")

    responses = Responses()
    client = OpenAIClient(
        enabled=True,
        max_calls=1,
        client=SimpleNamespace(responses=responses),
    )
    with pytest.raises(ProviderTransportError):
        client.complete_json(system="s", user="u", schema=INTENT_SELECTION_SCHEMA, name="intent")
    assert responses.calls == 1
    assert client.calls == 1


def test_two_invalid_structured_replies_are_exactly_two_calls() -> None:
    client = ScriptedLLM(replies=[{"target": "bad"}, {"query": "bad"}])
    policy = LLMIntentPolicy(client)
    with pytest.raises(LLMError):
        policy.choose_intent(_menu("intent:actor", "intent:source"))
    assert client.calls == 2


class _NoDataTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def execute(
        self, _case: object, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        self.calls += 1
        return make_observation(gap=gap, capability=self.name, target=target, payload={})


def _graph_fixture(
    capabilities: tuple[tuple[str, GapDimension], ...],
) -> tuple[InMemorySource, Diagnosis, tuple[ObservationCandidate, ...]]:
    onset = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    source = InMemorySource(
        name="a6-6-graph",
        alert_items=[Alert(name="latency", service="api", namespace="shop", starts_at=onset)],
        cutoff=onset,
    )
    case = build_case(source)
    base = diagnose_case(case)
    candidates = tuple(
        ObservationCandidate(
            candidate_id=f"candidate-{index}",
            capability=capability,
            target=EntityRef(kind="Deployment", name=f"target-{index}", namespace="shop"),
            query=InvestigationQuery(start=onset, end=onset),
            gap_ids=(f"gap-{index}",),
            dimensions=(dimension,),
            hypothesis_ids=(),
            alternative_ids=(f"alt-{index}",),
            discriminators=(
                CandidateDiscriminator(
                    gap_id=f"gap-{index}",
                    dimension=dimension,
                    missing_fact="bounded fact",
                    support_outcomes=(
                        GapOutcome(
                            kind=GapOutcomeKind.SUPPORTS,
                            alternative_ids=(f"alt-{index}",),
                            condition="positive target observation",
                            implication="supports this state",
                        ),
                    ),
                    comparison_hypothesis_ids=(),
                    comparison_alternative_ids=(f"other-alt-{index}",),
                    no_data_outcomes=(),
                ),
            ),
        )
        for index, (capability, dimension) in enumerate(capabilities)
    )
    gaps = tuple(
        InformationGap(
            gap_id=candidate.gap_ids[0],
            dimension=candidate.dimensions[0],
            alternative_ids=candidate.alternative_ids,
            missing_fact="bounded fact",
            discriminating_outcomes=(
                GapOutcome(
                    kind=GapOutcomeKind.SUPPORTS,
                    alternative_ids=candidate.alternative_ids,
                    condition="positive target observation",
                    implication="supports this state",
                ),
            ),
            authorized_queries=(
                AuthorizedQuery(capability=candidate.capability, target=candidate.target),
            ),
            candidate_tools=(candidate.capability,),
            resolvability=GapResolvability.RESOLVABLE,
        )
        for candidate in candidates
    )
    return source, base.model_copy(update={"information_gaps": gaps}), candidates


def test_graph_single_intent_bypasses_model(monkeypatch: pytest.MonkeyPatch) -> None:
    source, diagnosis, candidates = _graph_fixture((("events", GapDimension.EVENT_SEQUENCE),))
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: candidates,
    )
    client = ScriptedLLM(replies=[])
    tool = _NoDataTool("events")
    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(client),
        tools={"events": tool},
        config=InvestigationConfig(max_turns=1),
    )
    assert client.calls == 0
    assert tool.calls == 1
    assert result.tool_calls == 1
    assert result.action_audits[0].discriminator is not None
    assert result.action_audits[0].discriminator.gap_id == result.action_audits[0].action.gap_id
    assert result.action_audits[0].discriminator.no_data_is_discriminating is False


def test_action_without_candidate_discriminator_is_rejected_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, diagnosis, candidates = _graph_fixture((("events", GapDimension.EVENT_SEQUENCE),))
    candidate = candidates[0]
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: (replace(candidate, discriminators=()),),
    )
    tool = _NoDataTool("events")
    policy = ScriptedInvestigationPolicy(
        actions=[
            InvestigationAction(
                action="inspect",
                gap_id=candidate.gap_ids[0],
                capability=candidate.capability,
                target=candidate.target,
                query=candidate.query,
                rationale="attempt a legal but non-discriminating read",
            )
        ]
    )

    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=policy,
        tools={"events": tool},
        config=InvestigationConfig(max_turns=1),
    )

    assert result.tool_calls == 0
    assert tool.calls == 0
    assert result.action_audits[0].authorization_result == "REJECTED"
    assert result.action_audits[0].backend_execution_status.value == "NOT_EXECUTED"
    assert "positive discriminator" in result.action_audits[0].authorization_reason
    assert not any("deterministic-fallback" in step.detail for step in result.diagnosis.steps)


def test_discovery_no_data_is_audited_as_neutral_without_elimination() -> None:
    from packages.rca.investigation.candidates import build_observation_candidates

    onset = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    source = InMemorySource(
        name="discovery-neutral",
        alert_items=[Alert(name="latency", service="api", namespace="shop", starts_at=onset)],
        cutoff=onset,
    )
    initial_case = build_case(initial_view(source))
    initial = diagnose_case(initial_case)
    gap = next(
        item
        for item in initial.information_gaps
        if item.origin is InformationGapOrigin.DISCOVERY
        and item.dimension is GapDimension.EVENT_SEQUENCE
    )
    diagnosis = initial
    candidate = next(
        item
        for item in build_observation_candidates(
            case=initial_case,
            diagnosis=diagnosis,
            engine_config=EngineConfig(),
        )
        if gap.gap_id in item.gap_ids and item.capability == "incident_events"
    )
    assert candidate.discriminators
    action = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability=candidate.capability,
        target=candidate.target,
        query=candidate.query,
    )
    tool = _NoDataTool("incident_events")

    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=initial_case,
        policy=ScriptedInvestigationPolicy(actions=[action]),
        tools={"incident_events": tool},
        config=InvestigationConfig(max_turns=1),
    )

    audit = result.action_audits[0]
    assert tool.calls == 1
    assert audit.discriminator is not None
    assert audit.discriminator.kind.value == "DISCOVERY_DISCRIMINATION"
    assert audit.observation_outcome is GapOutcomeKind.NO_DATA
    assert audit.discriminator.no_data_is_discriminating is False
    assert audit.decision_state_changed is False
    assert audit.resolution_before == audit.resolution_after
    assert audit.hypothesis_states_before == audit.hypothesis_states_after
    assert result.diagnosis.resolution == diagnosis.resolution


def test_deterministic_selector_comparison_is_persisted_in_action_audit() -> None:
    onset = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    source = InMemorySource(
        name="selection-comparison-audit",
        alert_items=[Alert(name="latency", service="api", namespace="shop", starts_at=onset)],
        cutoff=onset,
    )
    result = investigate_diagnosis(
        source,
        policy=DeterministicIntentPolicy(),
        tools={"incident_events": _NoDataTool("incident_events")},
        config=InvestigationConfig(max_turns=1),
    )

    audit = result.action_audits[0]
    assert audit.action.capability == "incident_events"
    assert audit.selection_strategy == "BASELINE_FALLBACK"
    assert audit.selection_reason in {
        "active_choice_does_not_prove_improvement",
        "active_choice_has_lower_expected_decision_impact",
    }
    assert audit.baseline_candidate_id
    assert audit.active_candidate_id
    assert audit.authorization_result == "AUTHORIZED"
    assert audit.backend_execution_status.value == "SUCCEEDED"


def test_graph_valid_intent_and_unknown_intent_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    source, diagnosis, candidates = _graph_fixture(
        (
            ("events", GapDimension.EVENT_SEQUENCE),
            ("logs", GapDimension.LOG_ERROR_PATTERN),
        )
    )
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: candidates,
    )
    log_client = ScriptedLLM(replies=[{"intent_id": "intent:dependency_error_inspection"}])
    tools = {"events": _NoDataTool("events"), "logs": _NoDataTool("logs")}
    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(log_client),
        tools=tools,
        config=InvestigationConfig(max_turns=1),
    )
    assert log_client.calls == 0
    assert sum(tool.calls for tool in tools.values()) == 1
    assert result.tool_calls == 1

    fallback_client = ScriptedLLM(replies=[{"intent_id": "stale-intent"}])
    fallback_tools = {"events": _NoDataTool("events"), "logs": _NoDataTool("logs")}
    fallback = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(fallback_client),
        tools=fallback_tools,
        config=InvestigationConfig(max_turns=1),
    )
    assert fallback_client.calls == 0
    assert fallback.tool_calls == 1
    assert sum(tool.calls for tool in fallback_tools.values()) == 1
    assert any("deterministic-top-intent" in step.detail for step in fallback.diagnosis.steps)


def test_graph_provider_failure_falls_back_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, diagnosis, candidates = _graph_fixture(
        (
            ("events", GapDimension.EVENT_SEQUENCE),
            ("logs", GapDimension.LOG_ERROR_PATTERN),
        )
    )
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: candidates,
    )

    class FailingClient:
        model = "scripted"
        calls = 0

        def complete_json(self, **_: object) -> dict[str, object]:
            self.calls += 1
            raise LLMError("provider unavailable")

    client = FailingClient()
    tools = {"events": _NoDataTool("events"), "logs": _NoDataTool("logs")}
    result = investigate_diagnosis(
        source,
        diagnosis=diagnosis,
        initial_case=build_case(source),
        policy=LLMIntentPolicy(client),
        tools=tools,
        config=InvestigationConfig(max_turns=1),
    )
    assert client.calls == 0
    assert result.tool_calls == 1
    assert result.stop_reason.value != "MODEL_FAILURE"
    assert any("deterministic-top-intent" in step.detail for step in result.diagnosis.steps)


def test_intent_history_checkpoint_is_data_only_and_credential_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-secret-never-expose")
    source = InMemorySource(name="checkpoint-intent")
    state = build_investigation_state(source)
    state["intent_history"] = (
        {
            "intent_kind": "ACTOR_STATE_INSPECTION",
            "capability": "events",
            "outcome": "NO_DATA",
            "returned_refs": 0,
            "new_refs": 0,
            "findings": 0,
            "decision_state_changed": False,
        },
    )
    encoded = JsonPlusSerializer(pickle_fallback=False).dumps_typed(state)
    restored = JsonPlusSerializer(pickle_fallback=False).loads_typed(encoded)
    assert tuple(restored["intent_history"]) == tuple(state["intent_history"])
    assert b"sentinel-secret-never-expose" not in repr(encoded).encode()
