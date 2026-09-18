"""Low-level LangGraph orchestration for bounded evidence acquisition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import action_identity, validate_action
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    normalize_observation,
)
from packages.rca.investigation.state import (
    CaseRebuilder,
    InvestigationConfig,
    InvestigationPolicy,
    InvestigationPolicyContext,
    InvestigationState,
    InvestigationTool,
)
from packages.rca.investigation.tools import default_tools, make_observation
from packages.rca.llm import LLMError
from packages.rca.model import (
    Diagnosis,
    Finding,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationResult,
    InvestigationStep,
    InvestigationStopReason,
    Resolution,
)
from packages.rca.source import ObservationSource


def _engine_config(config: InvestigationConfig) -> EngineConfig:
    return config.engine or EngineConfig()


@dataclass(frozen=True)
class _DefaultCaseRebuilder:
    engine_config: EngineConfig

    def __call__(self, case: Case, findings: tuple[Finding, ...]) -> Case:
        return build_case(case.source, self.engine_config, extra_findings=findings)


def _default_rebuilder(config: InvestigationConfig) -> CaseRebuilder:
    return _DefaultCaseRebuilder(_engine_config(config))


def _resolvable_gaps(diagnosis: Diagnosis) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
    )


def _policy_calls(policy: InvestigationPolicy) -> int:
    if not getattr(policy, "counts_as_model", False):
        return 0
    client = getattr(policy, "client", None)
    return int(getattr(client, "calls", 0))


def _with_step(
    state: InvestigationState, action: str, detail: str
) -> tuple[InvestigationStep, ...]:
    return (
        *state.get("trace_steps", ()),
        InvestigationStep(actor="investigator", action=action, detail=detail[:500]),
    )


def _assess(state: InvestigationState) -> dict[str, Any]:
    diagnosis = state["current_diagnosis"]
    if diagnosis.resolution is Resolution.RESOLVED:
        return {
            "stop_reason": InvestigationStopReason.RESOLVED,
            "trace_steps": _with_step(
                state, "assess", "deterministic resolution is already RESOLVED"
            ),
        }
    if not _resolvable_gaps(diagnosis):
        return {
            "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
            "trace_steps": _with_step(state, "assess", "no resolvable information gap remains"),
        }
    if state["turns"] >= state["config"].max_turns:
        return {
            "stop_reason": InvestigationStopReason.TURN_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "turn budget exhausted"),
        }
    elapsed = (datetime.now(UTC) - state["started_at"]).total_seconds()
    if elapsed >= state["config"].max_wall_time_seconds:
        return {
            "stop_reason": InvestigationStopReason.WALL_TIME_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "wall-time budget exhausted"),
        }
    if state["model_calls"] >= state["config"].max_model_calls and getattr(
        state["policy"], "counts_as_model", False
    ):
        return {
            "stop_reason": InvestigationStopReason.MODEL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "model-call budget exhausted"),
        }
    if state["tool_calls"] >= state["config"].max_tool_calls:
        return {
            "stop_reason": InvestigationStopReason.TOOL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "tool-call budget exhausted"),
        }
    return {"stop_reason": None}


def _route_after_assess(state: InvestigationState) -> str:
    return "finalize" if state.get("stop_reason") is not None else "select_action"


def _select_action(state: InvestigationState) -> dict[str, Any]:
    diagnosis = state["current_diagnosis"]
    gaps = _resolvable_gaps(diagnosis)
    context = InvestigationPolicyContext(
        incident_id=state["incident_id"],
        diagnosis=diagnosis,
        hypotheses=tuple(diagnosis.ambiguous_hypotheses or diagnosis.alternative_hypotheses[:8]),
        gaps=gaps,
        attempted_actions=state["attempted_actions"][-16:],
        turns=state["turns"],
        model_calls_remaining=max(0, state["config"].max_model_calls - state["model_calls"]),
        tool_calls_remaining=max(0, state["config"].max_tool_calls - state["tool_calls"]),
    )
    before = _policy_calls(state["policy"])
    try:
        action = state["policy"].choose_action(context)
    except LLMError as error:
        return {
            "stop_reason": InvestigationStopReason.MODEL_FAILURE,
            "turns": state["turns"] + 1,
            "model_calls": state["model_calls"] + max(0, _policy_calls(state["policy"]) - before),
            "trace_steps": _with_step(state, "model_error", str(error)),
        }
    after = _policy_calls(state["policy"])
    return {
        "pending_action": action,
        "stop_reason": None,
        "turns": state["turns"] + 1,
        "model_calls": state["model_calls"] + max(0, after - before),
        "trace_steps": _with_step(
            state,
            "select_action",
            f"{action.action} {action.capability or ''} {action.target or ''}: {action.rationale}",
        ),
    }


def _route_after_select(state: InvestigationState) -> str:
    return "finalize" if state.get("stop_reason") is not None else "validate_action"


def _validate(state: InvestigationState) -> dict[str, Any]:
    action = state.get("pending_action")
    if action is None:
        return {
            "stop_reason": InvestigationStopReason.POLICY_STOP,
            "trace_steps": _with_step(state, "rejected", "policy returned no action"),
        }
    result = validate_action(
        action,
        gaps=_resolvable_gaps(state["current_diagnosis"]),
        tools=state["tools"],
        attempted_actions=state["attempted_actions"],
        tool_calls=state["tool_calls"],
        config=state["config"],
    )
    if not result.valid:
        invalid = state["invalid_actions"] + 1
        stop = (
            InvestigationStopReason.POLICY_STOP
            if invalid >= state["config"].max_invalid_actions
            else None
        )
        return {
            "invalid_actions": invalid,
            "rejected_actions": state["rejected_actions"] + 1,
            "stop_reason": stop,
            "trace_steps": _with_step(state, "rejected", result.reason),
        }
    if action.action == "stop":
        return {
            "stop_reason": InvestigationStopReason.POLICY_STOP,
            "trace_steps": _with_step(state, "policy_stop", action.rationale),
        }
    identity = action_identity(action)
    return {
        "attempted_actions": (*state["attempted_actions"], identity),
        "attempted_gap_ids": tuple(
            dict.fromkeys((*state["attempted_gap_ids"], action.gap_id or ""))
        ),
        "stop_reason": None,
        "trace_steps": _with_step(state, "validate_action", "allowed read-only action"),
    }


def _route_after_validate(state: InvestigationState) -> str:
    if state.get("stop_reason") is not None:
        return "finalize"
    return "execute_tool"


def _execute_tool(state: InvestigationState) -> dict[str, Any]:
    action = state["pending_action"]
    assert action is not None and action.gap_id is not None and action.capability is not None
    gap = next(
        gap for gap in _resolvable_gaps(state["current_diagnosis"]) if gap.gap_id == action.gap_id
    )
    assert action.target is not None
    tool = state["tools"][action.capability]
    try:
        observation = tool.execute(state["current_case"], gap, action.target)
    except Exception as error:  # semantic tools must not crash the diagnosis
        observation = make_observation(
            gap=gap,
            capability=action.capability,
            target=action.target,
            payload={},
            source_class="tool_error",
            error=f"{type(error).__name__}: {error}",
        )
    return {
        "pending_observation": observation,
        "tool_calls": state["tool_calls"] + 1,
        "trace_steps": _with_step(
            state,
            "execute_tool",
            f"{action.capability}({action.target.canonical}) → {observation.outcome.value}",
        ),
    }


def _normalize(state: InvestigationState) -> dict[str, Any]:
    observation = state.get("pending_observation")
    if observation is None:
        return {"last_new_evidence_count": 0}
    gap = next(
        (
            gap
            for gap in state["current_diagnosis"].information_gaps
            if gap.gap_id == observation.gap_id
        ),
        None,
    )
    if gap is None:
        return {
            "observations": (*state["observations"], observation),
            "last_new_evidence_count": 0,
            "trace_steps": _with_step(state, "normalize", "observation gap no longer exists"),
        }
    normalized = normalize_observation(observation, case=state["current_case"], gap=gap)
    fresh_findings = _new_investigation_findings(
        (*state["current_case"].findings, *state["investigation_findings"]),
        normalized.findings,
    )
    existing_ids = {item.observation_id for item in state["observations"]}
    observations = (
        state["observations"]
        if observation.observation_id in existing_ids
        else (*state["observations"], normalized.observation)
    )
    return {
        "observations": observations,
        "pending_findings": fresh_findings,
        "last_new_evidence_count": len(fresh_findings),
        "trace_steps": _with_step(
            state,
            "normalize",
            f"{len(normalized.findings)} deterministic finding(s) from {observation.observation_id}",
        ),
    }


def _new_investigation_findings(
    existing: tuple[Finding, ...], incoming: tuple[Finding, ...]
) -> tuple[Finding, ...]:
    """Avoid treating repeated normalized evidence as progress."""
    known_ids = {evidence_id for finding in existing for evidence_id in finding.evidence_ids}
    known_fallbacks = {
        (finding.entity.canonical, finding.kind.value, finding.summary) for finding in existing
    }
    fresh: list[Finding] = []
    for finding in deduplicate_findings(incoming):
        evidence_ids = set(finding.evidence_ids)
        fallback = (finding.entity.canonical, finding.kind.value, finding.summary)
        if evidence_ids and evidence_ids <= known_ids:
            continue
        if not evidence_ids and fallback in known_fallbacks:
            continue
        fresh.append(finding)
        known_ids.update(evidence_ids)
        known_fallbacks.add(fallback)
    return tuple(fresh)


def _rebuild(state: InvestigationState) -> dict[str, Any]:
    pending = state.get("pending_findings", ())
    combined = deduplicate_findings((*state["investigation_findings"], *pending))
    case = state["rebuild_case"](state["base_case"], combined)
    diagnosis = diagnose_case(case, config=_engine_config(state["config"]))
    return {
        "current_case": case,
        "current_diagnosis": diagnosis,
        "investigation_findings": combined,
        "trace_steps": _with_step(state, "rebuild", f"resolution={diagnosis.resolution.value}"),
    }


def _route_after_rebuild(state: InvestigationState) -> str:
    return "check_progress"


def _check_progress(state: InvestigationState) -> dict[str, Any]:
    current = state["current_diagnosis"]
    previous = state.get("previous_resolution", state["initial_diagnosis"].resolution)
    resolvable = _resolvable_gaps(current)
    no_progress = state["no_progress_count"]
    if (
        state["last_new_evidence_count"] == 0
        and current.resolution is previous
        and tuple(gap.gap_id for gap in resolvable)
        == tuple(gap.gap_id for gap in _resolvable_gaps(state["initial_diagnosis"]))
    ):
        no_progress += 1
    else:
        no_progress = 0
    stop: InvestigationStopReason | None = None
    if current.resolution is Resolution.RESOLVED:
        stop = InvestigationStopReason.RESOLVED
    elif not resolvable:
        stop = InvestigationStopReason.NO_RESOLVABLE_GAP
    elif no_progress >= state["config"].max_no_progress_rounds:
        stop = InvestigationStopReason.NO_PROGRESS
    elif state["turns"] >= state["config"].max_turns:
        stop = InvestigationStopReason.TURN_BUDGET_EXHAUSTED
    elif (datetime.now(UTC) - state["started_at"]).total_seconds() >= state[
        "config"
    ].max_wall_time_seconds:
        stop = InvestigationStopReason.WALL_TIME_EXHAUSTED
    elif state["tool_calls"] >= state["config"].max_tool_calls:
        stop = InvestigationStopReason.TOOL_BUDGET_EXHAUSTED
    elif state["model_calls"] >= state["config"].max_model_calls and getattr(
        state["policy"], "counts_as_model", False
    ):
        stop = InvestigationStopReason.MODEL_BUDGET_EXHAUSTED
    return {
        "previous_resolution": current.resolution,
        "no_progress_count": no_progress,
        "stop_reason": stop,
        "trace_steps": _with_step(
            state,
            "check_progress",
            f"new evidence={state['last_new_evidence_count']}; no-progress={no_progress}",
        ),
    }


def _route_after_progress(state: InvestigationState) -> str:
    return "finalize" if state.get("stop_reason") is not None else "select_action"


def _finalize(state: InvestigationState) -> dict[str, Any]:
    reason = state.get("stop_reason") or InvestigationStopReason.NO_PROGRESS
    diagnosis = state["current_diagnosis"].model_copy(
        update={
            "mode": "bounded-investigation",
            "model_calls": state["model_calls"],
            "steps": (*state["current_diagnosis"].steps, *state.get("trace_steps", ())),
        }
    )
    evidence_refs = tuple(
        dict.fromkeys(
            ref for finding in state["investigation_findings"] for ref in finding.evidence_ids
        )
    )
    result = InvestigationResult(
        diagnosis=diagnosis,
        initial_resolution=state["initial_diagnosis"].resolution,
        final_resolution=diagnosis.resolution,
        turns=state["turns"],
        model_calls=state["model_calls"],
        tool_calls=state["tool_calls"],
        unique_observations=len(state["observations"]),
        unique_evidence_added=len(evidence_refs),
        attempted_gap_ids=state["attempted_gap_ids"],
        rejected_actions=state["rejected_actions"],
        no_data_observations=sum(
            item.outcome is GapOutcomeKind.NO_DATA for item in state["observations"]
        ),
        stop_reason=reason,
        observations=state["observations"],
        new_evidence_refs=evidence_refs,
        resolved_during_investigation=(
            state["initial_diagnosis"].resolution is not Resolution.RESOLVED
            and diagnosis.resolution is Resolution.RESOLVED
        ),
    )
    return {"current_diagnosis": diagnosis, "final_result": result, "stop_reason": reason}


def build_investigation_graph(
    *,
    policy: InvestigationPolicy,
    tools: Mapping[str, InvestigationTool] | None = None,
    config: InvestigationConfig | None = None,
    checkpointer: Any | None = None,
    interrupt_before: tuple[str, ...] = (),
    interrupt_after: tuple[str, ...] = (),
) -> Any:
    """Build the explicit bounded graph with an injectable checkpointer."""
    graph = StateGraph(InvestigationState)
    graph.add_node("assess", _assess)
    graph.add_node("select_action", _select_action)
    graph.add_node("validate_action", _validate)
    graph.add_node("execute_tool", _execute_tool)
    graph.add_node("normalize_observation", _normalize)
    graph.add_node("rebuild_hypotheses", _rebuild)
    graph.add_node("check_progress", _check_progress)
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "assess")
    graph.add_conditional_edges(
        "assess", _route_after_assess, {"select_action": "select_action", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "select_action",
        _route_after_select,
        {"validate_action": "validate_action", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "validate_action",
        _route_after_validate,
        {"execute_tool": "execute_tool", "finalize": "finalize"},
    )
    graph.add_edge("execute_tool", "normalize_observation")
    graph.add_edge("normalize_observation", "rebuild_hypotheses")
    graph.add_edge("rebuild_hypotheses", "check_progress")
    graph.add_conditional_edges(
        "check_progress",
        _route_after_progress,
        {"select_action": "select_action", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    saver = checkpointer or InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=True))
    return graph.compile(
        checkpointer=saver,
        interrupt_before=list(interrupt_before),
        interrupt_after=list(interrupt_after),
    )


def investigate_diagnosis(
    source: ObservationSource,
    *,
    diagnosis: Diagnosis | None = None,
    policy: InvestigationPolicy,
    config: InvestigationConfig | None = None,
    tools: Mapping[str, InvestigationTool] | None = None,
    checkpointer: Any | None = None,
    thread_id: str | None = None,
    initial_case: Case | None = None,
    rebuild_case: CaseRebuilder | None = None,
) -> InvestigationResult:
    """Run one isolated bounded investigation and return deterministic output."""
    state = build_investigation_state(
        source,
        diagnosis=diagnosis,
        policy=policy,
        config=config,
        tools=tools,
        initial_case=initial_case,
        rebuild_case=rebuild_case,
    )
    graph = build_investigation_graph(
        policy=policy,
        tools=tools,
        config=config,
        checkpointer=checkpointer,
    )
    thread = thread_id or f"{source.incident_id()}:investigation"
    result = graph.invoke(state, config={"configurable": {"thread_id": thread}})
    final = result.get("final_result")
    if isinstance(final, InvestigationResult):
        return final
    raise RuntimeError("investigation graph terminated without a result")


def build_investigation_state(
    source: ObservationSource,
    *,
    diagnosis: Diagnosis | None = None,
    policy: InvestigationPolicy,
    config: InvestigationConfig | None = None,
    tools: Mapping[str, InvestigationTool] | None = None,
    initial_case: Case | None = None,
    rebuild_case: CaseRebuilder | None = None,
) -> InvestigationState:
    """Build a checkpointable initial state for direct graph or resume tests."""
    effective = config or InvestigationConfig()
    engine_config = _engine_config(effective)
    case = initial_case or build_case(source, engine_config)
    initial = diagnosis or diagnose_case(case, config=engine_config)
    return {
        "incident_id": source.incident_id(),
        "source": source,
        "config": effective,
        "policy": policy,
        "tools": dict(tools or default_tools()),
        "rebuild_case": rebuild_case or _default_rebuilder(effective),
        "started_at": datetime.now(UTC),
        "base_case": case,
        "current_case": case,
        "initial_diagnosis": initial,
        "current_diagnosis": initial,
        "observations": (),
        "investigation_findings": (),
        "attempted_actions": (),
        "attempted_gap_ids": (),
        "pending_action": None,
        "pending_observation": None,
        "pending_findings": (),
        "previous_resolution": initial.resolution,
        "turns": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "invalid_actions": 0,
        "rejected_actions": 0,
        "no_progress_count": 0,
        "last_new_evidence_count": 0,
        "stop_reason": None,
        "trace_steps": (),
        "final_result": None,
    }


def resume_investigation(graph: Any, *, thread_id: str) -> InvestigationResult:
    """Resume a checkpointed graph run without rebuilding its initial state."""
    result = graph.invoke(None, config={"configurable": {"thread_id": thread_id}})
    final = result.get("final_result")
    if isinstance(final, InvestigationResult):
        return final
    raise RuntimeError("investigation graph terminated without a result")


__all__ = [
    "InvestigationConfig",
    "build_investigation_graph",
    "build_investigation_state",
    "investigate_diagnosis",
    "resume_investigation",
]
