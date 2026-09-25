"""M19-3.15a: the trajectory replay contract a 1.1 investigation artifact carries."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import timedelta
from typing import Any

import pytest
from test_investigation import (
    _case,
    _CountingInvalidPolicy,
    _NoDataTool,
    _policy_action,
    _rebuild_with_findings,
)

from packages.rca.engine import EngineConfig, diagnose_case
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import (
    LLMIntentPolicy,
    LLMInvestigationPolicy,
    ScriptedInvestigationPolicy,
)
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.state import (
    InvestigationConfig,
    investigation_config_document,
    investigation_config_from_document,
    policy_kind,
)
from packages.rca.llm import OpenAIClient, ScriptedLLM
from packages.rca.model import (
    InvestigationAction,
    InvestigationPolicyKind,
    InvestigationResult,
    InvestigationStopReason,
    TrajectoryReplayContract,
)
from packages.rca.ranking import RankingConfig
from packages.storage.trajectory import trajectory_document

BUILT_IN = [
    (ScriptedInvestigationPolicy, InvestigationPolicyKind.ACTION),
    (LLMInvestigationPolicy, InvestigationPolicyKind.ACTION),
    (LLMIntentPolicy, InvestigationPolicyKind.INTENT_TIEBREAK),
    (DeterministicObservationPolicy, InvestigationPolicyKind.OBSERVATION_SELECTOR),
    (DeterministicIntentPolicy, InvestigationPolicyKind.INTENT_SELECTOR),
]


@pytest.mark.parametrize(("policy_class", "kind"), BUILT_IN)
def test_every_built_in_policy_family_declares_its_kind_explicitly(
    policy_class: type[Any], kind: InvestigationPolicyKind
) -> None:
    declared = {item.name: item for item in fields(policy_class)}
    assert "semantic_kind" in declared  # a real field, not the policy_kind() default
    assert declared["semantic_kind"].default is kind
    assert InvestigationPolicyKind.__members__[kind.value] is kind


def test_undeclared_policies_keep_the_action_path() -> None:
    assert policy_kind(object()) is InvestigationPolicyKind.ACTION
    assert policy_kind(_CountingInvalidPolicy()) is InvestigationPolicyKind.ACTION


def _every_field_changed() -> InvestigationConfig:
    ranking = RankingConfig()
    changed: dict[str, Any] = {}
    for item in fields(RankingConfig):
        value = getattr(ranking, item.name)
        if isinstance(value, timedelta):
            changed[item.name] = value + timedelta(seconds=7)
        elif isinstance(value, tuple):
            changed[item.name] = (*value, 0.25)
        elif isinstance(value, bool):
            changed[item.name] = not value
        else:
            changed[item.name] = value + 1
    engine = EngineConfig(
        ranking=replace(ranking, **changed),
        alternatives=7,
        pressure_baseline_gap=timedelta(minutes=9, microseconds=500),
        auxiliary_event_namespaces=("infra", "chaos-mesh"),
    )
    return InvestigationConfig(
        max_turns=3,
        max_model_calls=4,
        max_tool_calls=5,
        max_tool_calls_per_gap=1,
        max_invalid_actions=0,
        max_no_progress_rounds=9,
        max_wall_time_seconds=12.5,
        engine=engine,
    )


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    return [value]


def test_every_config_field_round_trips_through_json() -> None:
    config = _every_field_changed()
    default_engine = EngineConfig()
    for item in fields(InvestigationConfig):
        if item.name != "engine":
            assert getattr(config, item.name) != getattr(InvestigationConfig(), item.name)
    assert config.engine is not None and config.engine != default_engine
    document = json.loads(json.dumps(investigation_config_document(config)))
    assert investigation_config_from_document(document) == config
    # Plain JSON values only: no repr, class names or object identities.
    assert all(
        leaf is None or isinstance(leaf, (bool, int, float, str)) for leaf in _leaves(document)
    )
    assert "object at 0x" not in json.dumps(document)


def test_the_default_engine_is_recorded_resolved() -> None:
    document = investigation_config_document(InvestigationConfig())
    assert document["engine"] is not None
    assert investigation_config_from_document(document) == InvestigationConfig(
        engine=EngineConfig()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: {k: v for k, v in d.items() if k != "max_turns"}, id="missing"),
        pytest.param(lambda d: {**d, "extra": 1}, id="extra"),
        pytest.param(lambda d: {**d, "max_turns": "6"}, id="wrong-type"),
    ],
)
def test_malformed_config_documents_are_rejected(mutate: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        investigation_config_from_document(
            mutate(investigation_config_document(InvestigationConfig()))
        )


def _run(
    policy: Any, config: InvestigationConfig | None = None, **kwargs: Any
) -> InvestigationResult:
    case, _left, _right = _case()
    return investigate_diagnosis(
        case.source,
        diagnosis=diagnose_case(case),
        initial_case=case,
        policy=policy,
        config=config,
        rebuild_case=_rebuild_with_findings,
        **kwargs,
    )


def test_investigation_result_carries_the_contract_of_its_policy_and_config() -> None:
    config = InvestigationConfig(max_tool_calls=3)
    result = _run(ScriptedInvestigationPolicy([InvestigationAction(action="stop")]), config)
    contract = result.replay_contract
    assert contract is not None
    assert contract.policy_kind is InvestigationPolicyKind.ACTION
    assert contract.counts_as_model is False
    assert investigation_config_from_document(contract.config) == replace(
        config, engine=EngineConfig()
    )
    assert set(TrajectoryReplayContract.model_fields) == {
        "policy_kind",
        "counts_as_model",
        "config",
        "terminal",
    }
    # Run boundary metadata never enters the trajectory contract.
    dumped = json.dumps(contract.model_dump(mode="json"))
    for boundary_key in ("window_end", "snapshot_cycle_id", "provider_capabilities", "manifest"):
        assert boundary_key not in dumped
    llm = _run(LLMInvestigationPolicy(OpenAIClient(enabled=False, max_calls=1)))
    assert llm.replay_contract is not None and llm.replay_contract.counts_as_model is True
    tiebreak = _run(LLMIntentPolicy(ScriptedLLM([])))
    assert tiebreak.replay_contract is not None
    assert tiebreak.replay_contract.policy_kind is InvestigationPolicyKind.INTENT_TIEBREAK
    for policy_class, kind in BUILT_IN[3:]:
        selected = _run(policy_class())
        assert selected.replay_contract is not None
        assert selected.replay_contract.policy_kind is kind


def _terminal(result: InvestigationResult) -> tuple[Any, ...]:
    contract = result.replay_contract
    assert contract is not None
    terminal = contract.terminal
    assert terminal.stop_reason is result.stop_reason
    assert terminal.turns == result.turns
    assert terminal.audited_turns == len(result.action_audits)
    last = result.action_audits[-1].authorization_result if result.action_audits else None
    return (terminal.stop_reason, terminal.turns, terminal.audited_turns, last)


def test_terminal_families_are_distinct_in_the_persisted_contract() -> None:
    case, _left, right = _case()
    gap = next(
        gap for gap in diagnose_case(case).information_gaps if "events" in gap.candidate_tools
    )
    inspect = _policy_action(gap.gap_id, right)
    invalid = _policy_action(gap.gap_id, right, capability="logs")
    tools = {"events": _NoDataTool()}
    outcomes = {
        "policy stop": _run(ScriptedInvestigationPolicy([InvestigationAction(action="stop")])),
        "invalid actions exhausted": _run(
            ScriptedInvestigationPolicy([invalid]), InvestigationConfig(max_invalid_actions=1)
        ),
        "model failure": _run(LLMInvestigationPolicy(OpenAIClient(enabled=False, max_calls=1))),
        "wall time": _run(
            ScriptedInvestigationPolicy([inspect]),
            InvestigationConfig(max_wall_time_seconds=1e-9),
        ),
        "model budget": _run(
            _CountingInvalidPolicy(), InvestigationConfig(max_model_calls=1, max_invalid_actions=2)
        ),
        "tool budget": _run(
            ScriptedInvestigationPolicy([inspect, inspect]),
            InvestigationConfig(max_tool_calls=1, max_no_progress_rounds=99),
            tools=tools,
        ),
        "turn budget": _run(
            ScriptedInvestigationPolicy([inspect, inspect]),
            InvestigationConfig(max_turns=1, max_no_progress_rounds=99),
            tools=tools,
        ),
    }
    terminals = {name: _terminal(result) for name, result in outcomes.items()}
    assert terminals["policy stop"] == (InvestigationStopReason.POLICY_STOP, 1, 1, "POLICY_STOP")
    assert terminals["invalid actions exhausted"] == (
        InvestigationStopReason.POLICY_STOP,
        1,
        1,
        "REJECTED",
    )
    # The failed model turn left no audit: one more turn than audits.
    assert terminals["model failure"] == (InvestigationStopReason.MODEL_FAILURE, 1, 0, None)
    assert terminals["wall time"] == (InvestigationStopReason.WALL_TIME_EXHAUSTED, 0, 0, None)
    assert terminals["model budget"][0] is InvestigationStopReason.MODEL_BUDGET_EXHAUSTED
    assert terminals["tool budget"][0] is InvestigationStopReason.TOOL_BUDGET_EXHAUSTED
    assert terminals["turn budget"][0] is InvestigationStopReason.TURN_BUDGET_EXHAUSTED
    # No two terminal families share a persisted terminal state.
    assert len(set(terminals.values())) == len(terminals)
    # The audit-less terminals are distinct by the contract's terminal alone.
    audit_less = [
        terminals[name][:3]
        for name in ("model failure", "wall time", "model budget", "tool budget", "turn budget")
    ]
    assert len(set(audit_less)) == len(audit_less)


def test_a_result_without_a_contract_is_never_written_as_1_1() -> None:
    result = _run(ScriptedInvestigationPolicy([InvestigationAction(action="stop")]))
    assert trajectory_document(result)["replay_contract"] is not None
    with pytest.raises(ValueError, match="requires a replay contract"):
        trajectory_document(result.model_copy(update={"replay_contract": None}))
