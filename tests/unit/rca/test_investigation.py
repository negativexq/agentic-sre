from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.rca.engine import Case, diagnose_case
from packages.rca.hypotheses import hypothesis_candidate
from packages.rca.investigation.graph import (
    build_investigation_graph,
    build_investigation_state,
    investigate_diagnosis,
    resume_investigation,
)
from packages.rca.investigation.policy import LLMInvestigationPolicy, ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig, InvestigationPolicyContext
from packages.rca.investigation.tools import make_observation
from packages.rca.llm import ScriptedLLM
from packages.rca.model import (
    Alert,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    GapOutcomeKind,
    Hypothesis,
    HypothesisDiagnostics,
    InvestigationAction,
    InvestigationObservation,
    Resolution,
    Symptoms,
)
from packages.rca.ranking import Context
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    evidence_id: str,
    *,
    details: dict[str, Any] | None = None,
) -> Finding:
    onset = T0 + timedelta(minutes=2)
    at = T0 + timedelta(minutes=1)
    return Finding(
        kind=kind,
        entity=entity,
        at=at,
        incident_onset=onset,
        onset_delta_seconds=-60,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary=f"{kind.value} on {entity.canonical}",
        evidence_ids=(evidence_id,),
        details=details or {},
    )


def _case() -> tuple[Case, EntityRef, EntityRef]:
    left = _entity("HPA", "left")
    right = _entity("HPA", "right")
    left_finding = _finding(
        left,
        FindingKind.AUTOSCALING_FAILURE,
        "hpa:left",
        details={},
    )
    right_finding = _finding(
        right,
        FindingKind.AUTOSCALING_FAILURE,
        "hpa:right",
        details={},
    )
    hypotheses = [
        Hypothesis(
            hypothesis_id="hypothesis:left",
            causal_actor=left,
            members=(left,),
            findings=(left_finding,),
            initiating_findings=(left_finding,),
            linked_symptoms=("api",),
            causal_explanation="DIRECT",
            score=10,
        ),
        Hypothesis(
            hypothesis_id="hypothesis:right",
            causal_actor=right,
            members=(right,),
            findings=(right_finding,),
            initiating_findings=(right_finding,),
            linked_symptoms=("api",),
            causal_explanation="DIRECT",
            score=10,
        ),
    ]
    source = InMemorySource(
        name="investigation-synthetic",
        alert_items=[
            Alert(
                name="ApiLatency",
                service="api",
                namespace="shop",
                starts_at=T0 + timedelta(minutes=2),
            )
        ],
        cutoff=T0 + timedelta(minutes=10),
    )
    symptoms = Symptoms(
        onset=T0 + timedelta(minutes=2),
        last_seen=T0 + timedelta(minutes=10),
        services=("api",),
        namespaces=("shop",),
        alert_names=("ApiLatency",),
    )
    topology = Topology((), {})
    context = Context(symptoms=symptoms, symptom_entities=set(), topology=topology)
    candidates = [hypothesis_candidate(hypothesis) for hypothesis in hypotheses]
    case = Case(
        incident_id=source.incident_id(),
        source=source,
        symptoms=symptoms,
        topology=topology,
        context=context,
        findings=[left_finding, right_finding],
        candidates=candidates,
        hypotheses=hypotheses,
        hypothesis_diagnostics=HypothesisDiagnostics(),
    )
    return case, left, right


class _DiscriminatingEventsTool:
    name = "events"

    def __init__(self, finding: Finding) -> None:
        self.finding = finding
        self.calls = 0

    def execute(self, case: Case, gap: Any, target: EntityRef) -> InvestigationObservation:
        self.calls += 1
        return make_observation(
            gap=gap,
            capability=self.name,
            target=target,
            payload={"findings": [self.finding.model_dump(mode="json")]},
            evidence_refs=self.finding.evidence_ids,
            observed_at=self.finding.at,
        )


class _NoDataTool:
    name = "events"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, case: Case, gap: Any, target: EntityRef) -> InvestigationObservation:
        self.calls += 1
        return make_observation(gap=gap, capability=self.name, target=target, payload={})


def _rebuild_with_findings(base: Case, findings: tuple[Finding, ...]) -> Case:
    if not findings:
        return base
    right = base.hypotheses[1]
    enriched = right.model_copy(
        update={
            "hypothesis_id": "hypothesis:right:enriched",
            "findings": (*right.findings, *findings),
            "initiating_findings": (*right.initiating_findings, *findings),
            "score": right.score + 1,
        }
    )
    hypotheses = [enriched, base.hypotheses[0]]
    return replace(
        base,
        findings=[*base.findings, *findings],
        hypotheses=hypotheses,
        candidates=[hypothesis_candidate(item) for item in hypotheses],
    )


def _rebuild_with_left_contradiction(base: Case, findings: tuple[Finding, ...]) -> Case:
    if not findings:
        return base
    left = base.hypotheses[0].model_copy(
        update={
            "hypothesis_id": "hypothesis:left:contradicted",
            "findings": (*base.hypotheses[0].findings, *findings),
            "contradictory_findings": (
                *base.hypotheses[0].contradictory_findings,
                *findings,
            ),
        }
    )
    hypotheses = [base.hypotheses[1], left]
    return replace(
        base,
        findings=[*base.findings, *findings],
        hypotheses=hypotheses,
        candidates=[hypothesis_candidate(item) for item in hypotheses],
    )


def _policy_action(
    gap_id: str, target: EntityRef, capability: str = "events"
) -> InvestigationAction:
    return InvestigationAction(
        action="inspect",
        gap_id=gap_id,
        capability=capability,
        target=target,
        rationale="inspect the listed discriminating gap",
    )


def test_ambiguous_investigation_adds_evidence_and_resolves_deterministically() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    assert initial.resolution is Resolution.AMBIGUOUS
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    finding = _finding(
        right,
        FindingKind.CONFIG_CHANGE,
        "config:right",
        details={"changed_paths": ["spec.maxReplicas"]},
    )
    tool = _DiscriminatingEventsTool(finding)
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        policy=ScriptedInvestigationPolicy([_policy_action(gap.gap_id, right)]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.initial_resolution is Resolution.AMBIGUOUS
    assert result.final_resolution is Resolution.RESOLVED
    assert result.diagnosis.root_cause == right
    assert result.tool_calls == 1
    assert result.model_calls == 0
    assert result.resolved_during_investigation
    assert tool.calls == 1
    assert result.observations[0].outcome is GapOutcomeKind.SUPPORTS
    assert result.observations[0].hypothesis_ids


def test_investigation_observation_can_reveal_a_temporal_contradiction() -> None:
    case, left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    contradiction = _finding(left, FindingKind.CONFIG_CHANGE, "late:left").model_copy(
        update={
            "at": T0 + timedelta(hours=2),
            "onset_delta_seconds": 7180,
            "temporal_role": EvidenceTemporalRole.CONSEQUENCE,
        }
    )
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        policy=ScriptedInvestigationPolicy([_policy_action(gap.gap_id, left)]),
        tools={"events": _DiscriminatingEventsTool(contradiction)},
        rebuild_case=_rebuild_with_left_contradiction,
    )
    assert result.final_resolution is Resolution.RESOLVED
    assert result.diagnosis.root_cause == right
    assert result.diagnosis.resolution_trace is not None
    assert result.diagnosis.resolution_trace.decision_basis == "VALID_CONTRADICTION"


def test_no_data_is_neutral_and_does_not_resolve() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    tool = _NoDataTool()
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_no_progress_rounds=1),
        policy=ScriptedInvestigationPolicy([_policy_action(gap.gap_id, right)]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.final_resolution is Resolution.AMBIGUOUS
    assert result.stop_reason.value == "NO_PROGRESS"
    assert result.no_data_observations == 1
    assert result.new_evidence_refs == ()
    assert all(item.outcome is GapOutcomeKind.NO_DATA for item in result.observations)


def test_invalid_and_out_of_scope_actions_are_rejected_without_tool_execution() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    tool = _NoDataTool()
    invalid = _policy_action(gap.gap_id, right, capability="logs")
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_invalid_actions=1),
        policy=ScriptedInvestigationPolicy([invalid]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.tool_calls == 0
    assert result.rejected_actions == 1
    assert result.stop_reason.value == "POLICY_STOP"

    out_of_scope = _policy_action(gap.gap_id, _entity("HPA", "unrelated"))
    out_of_scope_result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_invalid_actions=1),
        policy=ScriptedInvestigationPolicy([out_of_scope]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert out_of_scope_result.tool_calls == 0
    assert out_of_scope_result.rejected_actions == 1


def test_resolved_case_skips_investigation() -> None:
    case, _left, _right = _case()
    resolved = diagnose_case(case).model_copy(update={"resolution": Resolution.RESOLVED})
    result = investigate_diagnosis(
        case.source,
        diagnosis=resolved,
        initial_case=case,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
    )
    assert result.model_calls == 0
    assert result.tool_calls == 0
    assert result.stop_reason.value == "RESOLVED"


def test_unresolvable_gap_skips_policy_and_model() -> None:
    case, _left, _right = _case()
    pressures = tuple(
        _finding(
            _entity("Pod", f"api-{side}"),
            FindingKind.RESOURCE_PRESSURE,
            f"pressure:api-{side}",
            details={"peak": 0.99},
        )
        for side in ("left", "right")
    )
    case.findings.extend(pressures)
    for index, pressure in enumerate(pressures):
        enriched = case.hypotheses[index].model_copy(
            update={"findings": (*case.hypotheses[index].findings, pressure)}
        )
        case.hypotheses[index] = enriched
        case.candidates[index] = hypothesis_candidate(enriched)
    diagnosis = diagnose_case(case)
    resource_gap = next(
        gap
        for gap in diagnosis.information_gaps
        if gap.dimension.value == "RESOURCE_PRESSURE" and not gap.candidate_tools
    )
    diagnosis = diagnosis.model_copy(update={"information_gaps": (resource_gap,)})
    result = investigate_diagnosis(
        case.source,
        diagnosis=diagnosis,
        initial_case=case,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
    )
    assert result.stop_reason.value == "NO_RESOLVABLE_GAP"
    assert result.model_calls == 0
    assert result.tool_calls == 0


def test_duplicate_action_is_blocked_and_loop_terminates() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    tool = _NoDataTool()
    action = _policy_action(gap.gap_id, right)
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_no_progress_rounds=99, max_invalid_actions=1),
        policy=ScriptedInvestigationPolicy([action, action]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.stop_reason.value == "POLICY_STOP"
    assert result.tool_calls == 1
    assert result.rejected_actions == 1


def test_duplicate_evidence_from_a_different_action_is_no_progress() -> None:
    case, left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    finding = _finding(right, FindingKind.AUTOSCALING_FAILURE, "hpa:right")
    tool = _DiscriminatingEventsTool(finding)
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_no_progress_rounds=2),
        policy=ScriptedInvestigationPolicy(
            [_policy_action(gap.gap_id, right), _policy_action(gap.gap_id, left)]
        ),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.final_resolution is Resolution.AMBIGUOUS
    assert result.stop_reason.value == "NO_PROGRESS"
    assert result.tool_calls == 2


def test_tool_budget_ends_a_persistently_ambiguous_run() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    tool = _NoDataTool()
    action = _policy_action(gap.gap_id, right)
    result = investigate_diagnosis(
        case.source,
        diagnosis=initial,
        initial_case=case,
        config=InvestigationConfig(max_tool_calls=1, max_no_progress_rounds=99),
        policy=ScriptedInvestigationPolicy([action, action]),
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    assert result.stop_reason.value == "TOOL_BUDGET_EXHAUSTED"
    assert result.final_resolution is Resolution.AMBIGUOUS


def test_action_schema_rejects_root_cause_and_mutation_requests() -> None:
    with pytest.raises(ValueError):
        InvestigationAction.model_validate({"action": "conclude", "root_cause": "shop/HPA/right"})
    with pytest.raises(ValueError):
        InvestigationAction.model_validate(
            {
                "action": "inspect",
                "gap_id": "gap:x",
                "capability": "delete",
                "target": "shop/HPA/right",
            }
        )


def test_llm_policy_only_returns_a_strict_observation_action() -> None:
    case, _left, right = _case()
    diagnosis = diagnose_case(case)
    gap = next(gap for gap in diagnosis.information_gaps if "events" in gap.candidate_tools)
    llm = ScriptedLLM(
        replies=[
            {
                "action": "inspect",
                "gap_id": gap.gap_id,
                "capability": "events",
                "target": right.model_dump(mode="json"),
                "rationale": "inspect the authorized gap",
            }
        ]
    )
    policy = LLMInvestigationPolicy(llm)
    action = policy.choose_action(
        InvestigationPolicyContext(
            incident_id=case.incident_id,
            diagnosis=diagnosis,
            hypotheses=tuple(diagnosis.ambiguous_hypotheses),
            gaps=(gap,),
            attempted_actions=(),
            turns=0,
            model_calls_remaining=1,
            tool_calls_remaining=1,
        )
    )
    assert action.action == "inspect"
    assert action.target == right
    assert not hasattr(action, "root_cause")
    assert llm.calls == 1


def test_checkpoint_resume_preserves_state_and_does_not_repeat_action() -> None:
    case, _left, right = _case()
    initial = diagnose_case(case)
    gap = next(gap for gap in initial.information_gaps if "events" in gap.candidate_tools)
    finding = _finding(right, FindingKind.CONFIG_CHANGE, "config:resume")
    tool = _DiscriminatingEventsTool(finding)
    policy = ScriptedInvestigationPolicy([_policy_action(gap.gap_id, right)])
    config = InvestigationConfig(max_no_progress_rounds=1)
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    saver = InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=True))
    graph = build_investigation_graph(
        policy=policy,
        tools={"events": tool},
        checkpointer=saver,
        interrupt_after=("normalize_observation",),
    )
    state = build_investigation_state(
        case.source,
        diagnosis=initial,
        initial_case=case,
        policy=policy,
        config=config,
        tools={"events": tool},
        rebuild_case=_rebuild_with_findings,
    )
    thread_id = "checkpoint-test"
    partial = graph.invoke(state, config={"configurable": {"thread_id": thread_id}})
    assert partial.get("final_result") is None
    result = resume_investigation(graph, thread_id=thread_id)
    assert result.final_resolution is Resolution.RESOLVED
    assert result.tool_calls == 1
    assert result.observations
    assert tool.calls == 1
