"""Low-level LangGraph orchestration for bounded evidence acquisition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.frontier import investigation_status
from packages.rca.information_gap import derive_information_gaps
from packages.rca.investigation.actions import action_identity, validate_action
from packages.rca.investigation.environment import (
    InvestigationBackend,
    initial_view,
    investigation_backend,
)
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    new_investigation_findings,
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
    FrontierStatus,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationActionStatus,
    InvestigationLedgerEntry,
    InvestigationResult,
    InvestigationStep,
    InvestigationStopReason,
    Resolution,
)
from packages.rca.resolution import resolve_hypotheses
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


class _Runtime:
    """Live dependencies of one graph, bound to its nodes and never checkpointed.

    The checkpoint holds only data (diagnoses, observations, findings, counters).
    The case is derived from the source plus the accumulated investigation
    findings, so a resumed thread rebuilds it instead of reading a pickled copy
    of a data source, an LLM client, or a callable.
    """

    def __init__(
        self,
        *,
        source: ObservationSource,
        policy: InvestigationPolicy,
        tools: Mapping[str, InvestigationTool] | None,
        config: InvestigationConfig | None,
        initial_case: Case | None,
        rebuild_case: CaseRebuilder | None,
        backend: InvestigationBackend | None,
    ) -> None:
        self.source = source
        self.policy = policy
        self.backend = backend or investigation_backend(source)
        self.tools: Mapping[str, InvestigationTool] = dict(tools or default_tools(self.backend))
        self.config = config or InvestigationConfig()
        self.engine_config = _engine_config(self.config)
        self.rebuild_case = rebuild_case or _default_rebuilder(self.config)
        self.base_case = initial_case or build_case(source, self.engine_config)
        self._cached: tuple[tuple[Finding, ...], Case] = ((), self.base_case)

    def case_for(self, findings: tuple[Finding, ...] | list[Finding]) -> Case:
        """The current case for these investigation findings, rebuilt on demand."""
        key = tuple(findings)
        if not key:
            return self.base_case
        if self._cached[0] == key:
            return self._cached[1]
        case = self.rebuild_case(self.base_case, key)
        self._cached = (key, case)
        return case


def _resolvable_gaps(diagnosis: Diagnosis) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
    )


def _frozen(value: Any) -> Any:
    """Restore tuples that a checkpoint round trip turned into lists."""
    if isinstance(value, list | tuple):
        return tuple(_frozen(item) for item in value)
    return value


def _gap_fingerprint(diagnosis: Diagnosis) -> tuple[tuple[str, ...], ...]:
    """Stable fingerprint for the currently investigable gap set."""
    return tuple(
        sorted(
            (
                gap.gap_id,
                gap.resolvability.value,
                *sorted(gap.candidate_tools),
                *sorted(gap.hypothesis_ids),
                *sorted(gap.alternative_ids),
            )
            for gap in _resolvable_gaps(diagnosis)
        )
    )


def _evidence_fingerprint(case: Case) -> tuple[str, ...]:
    """Stable identity of all effective findings in the current case."""
    return tuple(
        sorted({evidence_id for finding in case.findings for evidence_id in finding.evidence_ids})
    )


def _hypothesis_fingerprint(diagnosis: Diagnosis) -> tuple[str, ...]:
    """Track plausible/leading hypotheses independently of score ordering."""
    hypotheses = diagnosis.ambiguous_hypotheses or diagnosis.alternative_hypotheses
    if diagnosis.hypothesis is not None:
        hypotheses = (*hypotheses, diagnosis.hypothesis)
    return tuple(sorted({hypothesis.hypothesis_id for hypothesis in hypotheses}))


def _with_frontier_status(
    case: Case,
    diagnosis: Diagnosis,
    statuses: tuple[tuple[str, str], ...],
) -> Diagnosis:
    status_by_id = dict(statuses)
    alternatives = tuple(
        alternative.model_copy(
            update={
                "status": FrontierStatus(
                    status_by_id.get(alternative.alternative_id, alternative.status.value)
                )
            }
        )
        for alternative in diagnosis.structural_alternatives
    )
    gaps = derive_information_gaps(
        case.hypotheses,
        diagnosis.resolution_trace or resolve_hypotheses(case.hypotheses),
        case.source,
        structural_alternatives=alternatives,
    )
    return diagnosis.model_copy(
        update={
            "structural_alternatives": alternatives,
            "information_gaps": gaps,
            "investigation_status": investigation_status(
                alternatives,
                bounded=bool(getattr(case.source, "initial_observation_bounded", False)),
            ),
        }
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


def _assess(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    diagnosis = state["current_diagnosis"]
    if (
        diagnosis.resolution is Resolution.RESOLVED
        and diagnosis.investigation_status.value != "OPEN"
    ):
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
    if state["turns"] >= rt.config.max_turns:
        return {
            "stop_reason": InvestigationStopReason.TURN_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "turn budget exhausted"),
        }
    elapsed = (datetime.now(UTC) - state["started_at"]).total_seconds()
    if elapsed >= rt.config.max_wall_time_seconds:
        return {
            "stop_reason": InvestigationStopReason.WALL_TIME_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "wall-time budget exhausted"),
        }
    if state["model_calls"] >= rt.config.max_model_calls and getattr(
        rt.policy, "counts_as_model", False
    ):
        return {
            "stop_reason": InvestigationStopReason.MODEL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "model-call budget exhausted"),
        }
    if state["tool_calls"] >= rt.config.max_tool_calls:
        return {
            "stop_reason": InvestigationStopReason.TOOL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(state, "assess", "tool-call budget exhausted"),
        }
    return {"stop_reason": None}


def _route_after_assess(state: InvestigationState) -> str:
    return "finalize" if state.get("stop_reason") is not None else "select_action"


def _select_action(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    # Invalid-action retries bypass ``assess`` by design.  Re-check budgets at
    # this boundary so a malformed provider response (including its bounded
    # schema retry) cannot consume another model turn.
    if state["turns"] >= rt.config.max_turns:
        return {
            "stop_reason": InvestigationStopReason.TURN_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(
                state, "assess", "turn budget exhausted before action selection"
            ),
        }
    if state["tool_calls"] >= rt.config.max_tool_calls:
        return {
            "stop_reason": InvestigationStopReason.TOOL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(
                state, "assess", "tool-call budget exhausted before action selection"
            ),
        }
    if state["model_calls"] >= rt.config.max_model_calls and getattr(
        rt.policy, "counts_as_model", False
    ):
        return {
            "stop_reason": InvestigationStopReason.MODEL_BUDGET_EXHAUSTED,
            "trace_steps": _with_step(
                state, "assess", "model-call budget exhausted before action selection"
            ),
        }
    diagnosis = state["current_diagnosis"]
    gaps = _resolvable_gaps(diagnosis)
    context = InvestigationPolicyContext(
        incident_id=state["incident_id"],
        diagnosis=diagnosis,
        hypotheses=tuple(diagnosis.ambiguous_hypotheses or diagnosis.alternative_hypotheses[:8]),
        gaps=gaps,
        attempted_actions=state["attempted_actions"][-16:],
        turns=state["turns"],
        model_calls_remaining=max(0, rt.config.max_model_calls - state["model_calls"]),
        tool_calls_remaining=max(0, rt.config.max_tool_calls - state["tool_calls"]),
        previous_investigations=tuple(state.get("ledger", ())[-8:]),
        last_rejection=state.get("last_rejection"),
        structural_alternatives=diagnosis.structural_alternatives,
    )
    before = _policy_calls(rt.policy)
    try:
        action = rt.policy.choose_action(context)
    except LLMError as error:
        return {
            "stop_reason": InvestigationStopReason.MODEL_FAILURE,
            "turns": state["turns"] + 1,
            "model_calls": state["model_calls"] + max(0, _policy_calls(rt.policy) - before),
            "trace_steps": _with_step(state, "model_error", str(error)),
        }
    after = _policy_calls(rt.policy)
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


def _validate(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    action = state.get("pending_action")
    if action is None:
        return {
            "stop_reason": InvestigationStopReason.POLICY_STOP,
            "action_validation_status": InvestigationActionStatus.INVALID_EXHAUSTED,
            "trace_steps": _with_step(state, "rejected", "policy returned no action"),
        }
    result = validate_action(
        action,
        gaps=_resolvable_gaps(state["current_diagnosis"]),
        tools=rt.tools,
        attempted_actions=state["attempted_actions"],
        tool_calls=state["tool_calls"],
        config=rt.config,
    )
    if not result.valid:
        invalid = state["invalid_actions"] + 1
        stop = (
            InvestigationStopReason.POLICY_STOP
            if invalid >= rt.config.max_invalid_actions
            else None
        )
        return {
            "invalid_actions": invalid,
            "rejected_actions": state["rejected_actions"] + 1,
            "stop_reason": stop,
            "action_validation_status": (
                InvestigationActionStatus.INVALID_EXHAUSTED
                if stop is not None
                else InvestigationActionStatus.INVALID_RETRY
            ),
            "last_rejection": (
                result.reason,
                action.capability or "",
                action.target.canonical if action.target is not None else "",
            ),
            "trace_steps": _with_step(state, "rejected", result.reason),
        }
    if action.action == "stop":
        return {
            "stop_reason": InvestigationStopReason.POLICY_STOP,
            "action_validation_status": InvestigationActionStatus.VALID_STOP,
            "trace_steps": _with_step(state, "policy_stop", action.rationale),
        }
    identity = action_identity(action)
    return {
        "attempted_actions": (*state["attempted_actions"], identity),
        "attempted_gap_ids": tuple(
            dict.fromkeys((*state["attempted_gap_ids"], action.gap_id or ""))
        ),
        "stop_reason": None,
        "action_validation_status": InvestigationActionStatus.VALID_INSPECT,
        "last_rejection": None,
        "trace_steps": _with_step(state, "validate_action", "allowed read-only action"),
    }


def _route_after_validate(state: InvestigationState) -> str:
    status = state.get("action_validation_status")
    if status is InvestigationActionStatus.INVALID_RETRY:
        return "select_action"
    if (
        status
        in {
            InvestigationActionStatus.INVALID_EXHAUSTED,
            InvestigationActionStatus.VALID_STOP,
        }
        or state.get("stop_reason") is not None
    ):
        return "finalize"
    if status is InvestigationActionStatus.VALID_INSPECT:
        return "execute_tool"
    return "finalize"


def _execute_tool(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    action = state["pending_action"]
    assert action is not None and action.gap_id is not None and action.capability is not None
    gap = next(
        gap for gap in _resolvable_gaps(state["current_diagnosis"]) if gap.gap_id == action.gap_id
    )
    assert action.target is not None
    tool = rt.tools[action.capability]
    try:
        case = rt.case_for(state["investigation_findings"])
        execute_query = getattr(tool, "execute_query", None)
        if callable(execute_query):
            observation = execute_query(case, gap, action.target, action.query)
        else:
            observation = tool.execute(case, gap, action.target)
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


def _check_novelty(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    """Compare raw query references with effective case evidence before normalization."""
    observation = state.get("pending_observation")
    if observation is None:
        return {
            "pending_returned_evidence_refs": (),
            "pending_new_evidence_refs": (),
            "pending_already_known_refs": (),
            "last_new_raw_evidence_count": 0,
            "trace_steps": _with_step(state, "check_novelty", "no observation returned"),
        }
    current_case = rt.case_for(state["investigation_findings"])
    known_refs = {
        evidence_id
        for finding in (*current_case.findings, *state["investigation_findings"])
        for evidence_id in finding.evidence_ids
    }
    returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
    new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
    known = tuple(ref for ref in returned_refs if ref in known_refs)
    return {
        "pending_returned_evidence_refs": returned_refs,
        "pending_new_evidence_refs": new_refs,
        "pending_already_known_refs": known,
        "last_new_raw_evidence_count": len(new_refs),
        "trace_steps": _with_step(
            state,
            "check_novelty",
            f"returned={len(returned_refs)}; new={len(new_refs)}; already-known={len(known)}",
        ),
    }


def _normalize(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
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
    normalized = normalize_observation(
        observation, case=rt.case_for(state["investigation_findings"]), gap=gap
    )
    fresh_findings = new_investigation_findings(
        (*rt.case_for(state["investigation_findings"]).findings, *state["investigation_findings"]),
        normalized.findings,
    )
    returned_refs = state.get("pending_returned_evidence_refs", ())
    new_refs = state.get("pending_new_evidence_refs", ())
    known_refs = set(state.get("pending_already_known_refs", ()))
    finding_ids = tuple(
        f"{finding.kind.value}:{finding.entity.canonical}:{','.join(finding.evidence_ids)}"
        for finding in fresh_findings
    )
    action = state.get("pending_action")
    frontier = dict(state.get("frontier_status", ()))
    affected_alternatives = {
        alternative.alternative_id
        for alternative in state["current_diagnosis"].structural_alternatives
        if observation.target in alternative.observation_targets
        or observation.target == alternative.actor
    }
    promoted_actors = {finding.entity for finding in fresh_findings}
    for alternative in state["current_diagnosis"].structural_alternatives:
        if alternative.actor in promoted_actors:
            frontier[alternative.alternative_id] = FrontierStatus.PROMOTED.value
        elif alternative.alternative_id in affected_alternatives:
            frontier[alternative.alternative_id] = FrontierStatus.QUERIED_NO_CAUSAL_FINDING.value
    ledger_entry = InvestigationLedgerEntry(
        query_id=observation.observation_id,
        gap_id=observation.gap_id,
        capability=observation.capability,
        target=observation.target,
        query=action.query if action is not None else None,
        returned_evidence_refs=returned_refs,
        new_evidence_refs=new_refs,
        already_known_refs=tuple(ref for ref in returned_refs if ref in known_refs),
        normalized_finding_ids=finding_ids,
        affected_hypothesis_ids=normalized.observation.hypothesis_ids,
        outcome=normalized.observation.outcome,
    )
    existing_ids = {item.observation_id for item in state["observations"]}
    observations = (
        state["observations"]
        if observation.observation_id in existing_ids
        else (*state["observations"], normalized.observation)
    )
    return {
        "observations": observations,
        "ledger": (*state.get("ledger", ()), ledger_entry),
        "pending_findings": fresh_findings,
        "frontier_status": tuple(sorted(frontier.items())),
        "last_new_evidence_count": len(fresh_findings),
        "trace_steps": _with_step(
            state,
            "normalize",
            f"{len(normalized.findings)} deterministic finding(s) from {observation.observation_id}",
        ),
    }


def _rebuild(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    pending = state.get("pending_findings", ())
    combined = deduplicate_findings((*state["investigation_findings"], *pending))
    case = rt.case_for(combined)
    diagnosis = diagnose_case(case, config=rt.engine_config)
    diagnosis = _with_frontier_status(case, diagnosis, state.get("frontier_status", ()))
    return {
        "current_diagnosis": diagnosis,
        "investigation_findings": combined,
        "trace_steps": _with_step(state, "rebuild", f"resolution={diagnosis.resolution.value}"),
    }


def _route_after_rebuild(state: InvestigationState) -> str:
    return "check_progress"


def _check_progress(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    current = state["current_diagnosis"]
    current_resolution = current.resolution
    current_gap_fingerprint = _gap_fingerprint(current)
    current_evidence_fingerprint = _evidence_fingerprint(
        rt.case_for(state["investigation_findings"])
    )
    current_hypothesis_fingerprint = _hypothesis_fingerprint(current)
    previous = state.get("previous_resolution", state["initial_diagnosis"].resolution)
    no_progress = state["no_progress_count"]
    unchanged = (
        current_resolution is previous
        and current_gap_fingerprint == _frozen(state["previous_gap_fingerprint"])
        and current_evidence_fingerprint == _frozen(state["previous_evidence_fingerprint"])
        and current_hypothesis_fingerprint == _frozen(state["previous_hypothesis_fingerprint"])
    )
    if unchanged:
        no_progress += 1
    else:
        no_progress = 0
    resolvable = _resolvable_gaps(current)
    stop: InvestigationStopReason | None = None
    if current.resolution is Resolution.RESOLVED and current.investigation_status.value != "OPEN":
        stop = InvestigationStopReason.RESOLVED
    elif not resolvable:
        stop = InvestigationStopReason.NO_RESOLVABLE_GAP
    elif no_progress >= rt.config.max_no_progress_rounds:
        stop = InvestigationStopReason.NO_PROGRESS
    elif state["turns"] >= rt.config.max_turns:
        stop = InvestigationStopReason.TURN_BUDGET_EXHAUSTED
    elif (
        datetime.now(UTC) - state["started_at"]
    ).total_seconds() >= rt.config.max_wall_time_seconds:
        stop = InvestigationStopReason.WALL_TIME_EXHAUSTED
    elif state["tool_calls"] >= rt.config.max_tool_calls:
        stop = InvestigationStopReason.TOOL_BUDGET_EXHAUSTED
    elif state["model_calls"] >= rt.config.max_model_calls and getattr(
        rt.policy, "counts_as_model", False
    ):
        stop = InvestigationStopReason.MODEL_BUDGET_EXHAUSTED
    return {
        "previous_resolution": current.resolution,
        "previous_gap_fingerprint": current_gap_fingerprint,
        "previous_evidence_fingerprint": current_evidence_fingerprint,
        "previous_hypothesis_fingerprint": current_hypothesis_fingerprint,
        "no_progress_count": no_progress,
        "stop_reason": stop,
        "trace_steps": _with_step(
            state,
            "check_progress",
            f"new evidence={state['last_new_evidence_count']}; unchanged={unchanged}; "
            f"no-progress={no_progress}",
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
    # Count only references that crossed the initial/effective-case boundary;
    # a normalized finding may retain already-known provenance for explanation.
    evidence_refs = tuple(
        dict.fromkeys(ref for entry in state.get("ledger", ()) for ref in entry.new_evidence_refs)
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
        ledger=state.get("ledger", ()),
        new_evidence_refs=evidence_refs,
        resolved_during_investigation=(
            state["initial_diagnosis"].resolution is not Resolution.RESOLVED
            and diagnosis.resolution is Resolution.RESOLVED
        ),
    )
    return {"current_diagnosis": diagnosis, "final_result": result, "stop_reason": reason}


def build_investigation_graph(
    *,
    source: ObservationSource,
    policy: InvestigationPolicy,
    tools: Mapping[str, InvestigationTool] | None = None,
    config: InvestigationConfig | None = None,
    initial_case: Case | None = None,
    rebuild_case: CaseRebuilder | None = None,
    backend: InvestigationBackend | None = None,
    checkpointer: Any | None = None,
    interrupt_before: tuple[str, ...] = (),
    interrupt_after: tuple[str, ...] = (),
) -> Any:
    """Build the bounded graph; live dependencies are bound to nodes, not state.

    The default checkpointer refuses pickle, so any runtime object that leaks
    into state fails at the first checkpoint instead of being silently copied.
    """
    rt = _Runtime(
        source=source,
        policy=policy,
        tools=tools,
        config=config,
        initial_case=initial_case,
        rebuild_case=rebuild_case,
        backend=backend,
    )

    def bind(node: Callable[[InvestigationState, _Runtime], dict[str, Any]]) -> Any:
        def run(state: InvestigationState) -> dict[str, Any]:
            return node(state, rt)

        return run

    graph = StateGraph(InvestigationState)
    graph.add_node("assess", bind(_assess))
    graph.add_node("select_action", bind(_select_action))
    graph.add_node("validate_action", bind(_validate))
    graph.add_node("execute_tool", bind(_execute_tool))
    graph.add_node("check_novelty", bind(_check_novelty))
    graph.add_node("normalize_observation", bind(_normalize))
    graph.add_node("rebuild_hypotheses", bind(_rebuild))
    graph.add_node("check_progress", bind(_check_progress))
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
        {
            "select_action": "select_action",
            "execute_tool": "execute_tool",
            "finalize": "finalize",
        },
    )
    graph.add_edge("execute_tool", "check_novelty")
    graph.add_edge("check_novelty", "normalize_observation")
    graph.add_edge("normalize_observation", "rebuild_hypotheses")
    graph.add_edge("rebuild_hypotheses", "check_progress")
    graph.add_conditional_edges(
        "check_progress",
        _route_after_progress,
        {"select_action": "select_action", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    saver = checkpointer or InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False))
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
    initial_source = (
        source if initial_case is not None or diagnosis is not None else initial_view(source)
    )
    case = initial_case or build_case(
        initial_source, _engine_config(config or InvestigationConfig())
    )
    graph = build_investigation_graph(
        source=initial_source,
        policy=policy,
        tools=tools,
        config=config,
        initial_case=case,
        rebuild_case=rebuild_case,
        backend=investigation_backend(source),
        checkpointer=checkpointer,
    )
    state = build_investigation_state(
        initial_source, diagnosis=diagnosis, config=config, initial_case=case
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
    config: InvestigationConfig | None = None,
    initial_case: Case | None = None,
) -> InvestigationState:
    """Build the initial checkpointable state: data only, no live dependencies."""
    effective = config or InvestigationConfig()
    engine_config = _engine_config(effective)
    case = initial_case or build_case(source, engine_config)
    initial = diagnosis or diagnose_case(case, config=engine_config)
    return {
        "incident_id": source.incident_id(),
        "started_at": datetime.now(UTC),
        "initial_diagnosis": initial,
        "current_diagnosis": initial,
        "observations": (),
        "ledger": (),
        "investigation_findings": (),
        "attempted_actions": (),
        "attempted_gap_ids": (),
        "pending_action": None,
        "pending_observation": None,
        "pending_findings": (),
        "pending_returned_evidence_refs": (),
        "pending_new_evidence_refs": (),
        "pending_already_known_refs": (),
        "previous_resolution": initial.resolution,
        "previous_gap_fingerprint": _gap_fingerprint(initial),
        "previous_evidence_fingerprint": _evidence_fingerprint(case),
        "previous_hypothesis_fingerprint": _hypothesis_fingerprint(initial),
        "frontier_status": tuple(
            sorted(
                (item.alternative_id, item.status.value) for item in initial.structural_alternatives
            )
        ),
        "last_rejection": None,
        "action_validation_status": None,
        "turns": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "invalid_actions": 0,
        "rejected_actions": 0,
        "no_progress_count": 0,
        "last_new_evidence_count": 0,
        "last_new_raw_evidence_count": 0,
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
