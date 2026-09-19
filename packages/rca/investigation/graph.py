"""Low-level LangGraph orchestration for bounded evidence acquisition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.frontier import (
    apply_frontier_progress,
    covered_frontier_dimensions,
)
from packages.rca.investigation.actions import (
    action_identity,
    observation_identity,
    validate_action,
)
from packages.rca.investigation.environment import (
    InvestigationBackend,
    initial_view,
    investigation_backend,
)
from packages.rca.investigation.evidence import (
    InMemoryEvidenceStore,
    OverlayObservationSource,
    records_from_observation,
    visible_evidence_refs,
)
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    finding_identity,
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
    GapDimension,
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
from packages.rca.source import ObservationSource


def _engine_config(config: InvestigationConfig) -> EngineConfig:
    return config.engine or EngineConfig()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return sorted(
            items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if hasattr(value, "__slots__"):
        return {
            name: _canonical_value(getattr(value, name))
            for name in value.__slots__
            if hasattr(value, name)
        }
    if hasattr(value, "__dict__"):
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.__dict__.items())
            if key != "source"
        }
    return value


def world_model_fingerprint(case: Case) -> str:
    """Hash only deterministic evidence-derived products of a Case."""
    payload = {
        "topology": {
            "edges": case.topology.edges,
            "latest": case.topology.latest,
        },
        "findings": case.findings,
        "candidates": case.candidates,
        "hypotheses": case.hypotheses,
        "runtime_graph": case.runtime_graph,
        "runtime_evidence": case.runtime_evidence,
        "runtime_propagation": case.runtime_propagation,
        "hypothesis_causal_roles": case.hypothesis_causal_roles,
        "root_cause_eligibilities": case.root_cause_eligibilities,
        "runtime_mechanism_bridges": case.runtime_mechanism_bridges,
    }
    encoded = json.dumps(_canonical_value(payload), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _DefaultCaseRebuilder:
    engine_config: EngineConfig

    def __call__(self, source: ObservationSource, findings: tuple[Finding, ...]) -> Case:
        return build_case(source, self.engine_config, extra_findings=findings)


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
        rebuild_case: Callable[..., Case] | None,
        backend: InvestigationBackend | None,
        evidence_store: InMemoryEvidenceStore | None,
    ) -> None:
        self.base_source = source
        self.policy = policy
        self.backend = backend or investigation_backend(source)
        self.tools: Mapping[str, InvestigationTool] = dict(tools or default_tools(self.backend))
        source_capabilities = {
            "history",
            "events",
            "logs",
            "resource_pressure",
            "traffic",
            "runtime_traces",
        }
        self.supported_capabilities = frozenset(
            capability
            for capability in self.tools
            if capability not in source_capabilities or self.backend.supports(capability)
        )
        self.evidence_store = evidence_store or InMemoryEvidenceStore()
        access_ledger = getattr(source, "access_ledger", None)
        if callable(access_ledger):
            ledger = access_ledger()
            self.base_visible_refs = frozenset(ref for refs in ledger.values() for ref in refs)
        else:
            self.base_visible_refs = visible_evidence_refs(source)
        self.config = config or InvestigationConfig()
        self.engine_config = _engine_config(self.config)
        self.rebuild_case: Callable[..., Case] = rebuild_case or _default_rebuilder(self.config)
        self.base_case = initial_case or build_case(source, self.engine_config)
        self._cached: tuple[tuple[tuple[str, ...], tuple[Finding, ...]], Case] = (
            ((), ()),
            self.base_case,
        )

    def case_for(
        self,
        acquired_evidence_refs: tuple[str, ...],
        findings: tuple[Finding, ...] | list[Finding],
    ) -> Case:
        """The current case for acquired evidence and legacy findings."""
        refs = tuple(acquired_evidence_refs)
        for evidence_id in refs:
            if not self.evidence_store.contains(evidence_id):
                raise ValueError(f"missing acquired evidence ref {evidence_id}")
        key = (refs, tuple(findings))
        if not refs and not key[1]:
            return self.base_case
        if self._cached[0] == key:
            return self._cached[1]
        overlay = OverlayObservationSource(
            base=self.base_source,
            store=self.evidence_store,
            acquired_evidence_refs=refs,
            supported_capabilities=self.supported_capabilities,
        )
        try:
            case = self.rebuild_case(overlay, key[1])
        except AttributeError:
            # Preserve the pre-A1 test/custom callback contract while the
            # production default rebuilds from the overlay source.
            case = self.rebuild_case(self.base_case, key[1])
        self._cached = (key, case)
        return case


def _resolvable_gaps(diagnosis: Diagnosis) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.authorized_queries
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
                *sorted(
                    f"{query.capability}|{query.target.canonical}"
                    for query in gap.authorized_queries
                ),
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
        attempted_observations=state.get("attempted_observations", ()),
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
    assert action.target is not None and action.capability is not None
    read_identity = observation_identity(action.capability, action.target, action.query)
    return {
        "attempted_actions": (*state["attempted_actions"], identity),
        "attempted_observations": (
            *state.get("attempted_observations", ()),
            read_identity,
        ),
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
        case = rt.case_for(state.get("acquired_evidence_refs", ()), state["investigation_findings"])
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
    """Materialize native records and compare raw query references."""
    observation = state.get("pending_observation")
    if observation is None:
        return {
            "pending_returned_evidence_refs": (),
            "pending_new_evidence_refs": (),
            "pending_already_known_refs": (),
            "last_new_raw_evidence_count": 0,
            "trace_steps": _with_step(state, "check_novelty", "no observation returned"),
        }
    current_case = rt.case_for(
        state.get("acquired_evidence_refs", ()), state["investigation_findings"]
    )
    known_refs = set(rt.base_visible_refs)
    known_refs.update(state.get("acquired_evidence_refs", ()))
    known_refs.update(
        evidence_id
        for finding in (*current_case.findings, *state["investigation_findings"])
        for evidence_id in finding.evidence_ids
    )
    try:
        records = records_from_observation(observation)
    except ValueError as error:
        failed_observation = observation.model_copy(
            update={
                "payload": {},
                "evidence_refs": (),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return {
            "pending_observation": failed_observation,
            "pending_returned_evidence_refs": (),
            "pending_new_evidence_refs": (),
            "pending_already_known_refs": (),
            "last_new_raw_evidence_count": 0,
            "trace_steps": _with_step(state, "check_novelty", str(error)),
        }
    record_by_ref: dict[str, Any] = {}
    for record in records:
        prior = record_by_ref.get(record.evidence_id)
        if prior is not None:
            prior_payload = {
                "type": type(prior).__qualname__,
                "record": prior.model_dump(mode="json"),
            }
            record_payload = {
                "type": type(record).__qualname__,
                "record": record.model_dump(mode="json"),
            }
            if json.dumps(prior_payload, sort_keys=True, separators=(",", ":")) != json.dumps(
                record_payload, sort_keys=True, separators=(",", ":")
            ):
                raise ValueError(f"evidence ID collision for {record.evidence_id}")
            continue
        record_by_ref[record.evidence_id] = record
    returned_refs = tuple(dict.fromkeys((*observation.evidence_refs, *record_by_ref)))
    acquired = list(state.get("acquired_evidence_refs", ()))
    new_refs: list[str] = []
    known: list[str] = []
    for ref in returned_refs:
        candidate_record = record_by_ref.get(ref)
        if candidate_record is not None:
            base_known = ref in rt.base_visible_refs
            if base_known or ref in acquired:
                known.append(ref)
                continue
            rt.evidence_store.put(candidate_record)
            acquired.append(ref)
            new_refs.append(ref)
            continue
        if ref in known_refs:
            known.append(ref)
        else:
            new_refs.append(ref)
    return {
        "pending_returned_evidence_refs": returned_refs,
        "pending_new_evidence_refs": tuple(new_refs),
        "pending_already_known_refs": tuple(known),
        "acquired_evidence_refs": tuple(dict.fromkeys(acquired)),
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
    native_records = records_from_observation(observation)
    normalized_findings: tuple[Finding, ...]
    if native_records:
        normalized_observation = observation
        if not state.get("pending_new_evidence_refs", ()):
            legacy_result = normalize_observation(
                observation,
                case=rt.case_for(
                    state.get("acquired_evidence_refs", ()), state["investigation_findings"]
                ),
                gap=gap,
            )
            normalized_observation = legacy_result.observation
            if legacy_result.findings and normalized_observation.hypothesis_ids:
                normalized_observation = normalized_observation.model_copy(
                    update={"outcome": GapOutcomeKind.SUPPORTS}
                )
        normalized_findings = ()
    else:
        normalized_result = normalize_observation(
            observation,
            case=rt.case_for(
                state.get("acquired_evidence_refs", ()), state["investigation_findings"]
            ),
            gap=gap,
        )
        normalized_observation = normalized_result.observation
        normalized_findings = normalized_result.findings
    fresh_findings = new_investigation_findings(
        (
            *rt.case_for(
                state.get("acquired_evidence_refs", ()), state["investigation_findings"]
            ).findings,
            *state["investigation_findings"],
        ),
        normalized_findings,
    )
    returned_refs = state.get("pending_returned_evidence_refs", ())
    new_refs = state.get("pending_new_evidence_refs", ())
    known_refs = set(state.get("pending_already_known_refs", ()))
    finding_ids = tuple(
        f"{finding.kind.value}:{finding.entity.canonical}:{','.join(finding.evidence_ids)}"
        for finding in fresh_findings
    )
    action = state.get("pending_action")
    queried_dimensions = {
        alternative_id: {GapDimension(value) for value in dimensions}
        for alternative_id, dimensions in state.get("frontier_queried_dimensions", ())
    }
    if observation.error is None and observation.capability != "runtime_traces":
        covered = covered_frontier_dimensions(
            state["current_diagnosis"],
            capability=observation.capability,
            target=observation.target,
        )
        for alternative_id, dimensions in covered.items():
            queried_dimensions.setdefault(alternative_id, set()).update(dimensions)
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
        affected_hypothesis_ids=normalized_observation.hypothesis_ids,
        outcome=normalized_observation.outcome,
    )
    existing_ids = {item.observation_id for item in state["observations"]}
    observations = (
        state["observations"]
        if observation.observation_id in existing_ids
        else (*state["observations"], normalized_observation)
    )
    return {
        "observations": observations,
        "ledger": (*state.get("ledger", ()), ledger_entry),
        "pending_findings": fresh_findings,
        "frontier_queried_dimensions": tuple(
            (alternative_id, tuple(sorted(dimensions, key=lambda item: item.value)))
            for alternative_id, dimensions in sorted(queried_dimensions.items())
        ),
        "last_new_evidence_count": len(fresh_findings),
        "trace_steps": _with_step(
            state,
            "normalize",
            f"{len(normalized_findings)} deterministic finding(s) from {observation.observation_id}",
        ),
    }


def _rebuild(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    pending = state.get("pending_findings", ())
    combined = deduplicate_findings((*state["investigation_findings"], *pending))
    newly_acquired = set(state.get("pending_new_evidence_refs", ()))
    previous_refs = tuple(
        ref for ref in state.get("acquired_evidence_refs", ()) if ref not in newly_acquired
    )
    before_case = rt.case_for(previous_refs, state["investigation_findings"])
    case = rt.case_for(state.get("acquired_evidence_refs", ()), combined)
    queried_dimensions = {
        alternative_id: tuple(GapDimension(value) for value in dimensions)
        for alternative_id, dimensions in state.get("frontier_queried_dimensions", ())
    }
    case.structural_alternatives = list(
        apply_frontier_progress(
            case.structural_alternatives,
            hypotheses=case.hypotheses,
            queried_dimensions_by_alternative=queried_dimensions,
        )
    )
    diagnosis = diagnose_case(case, config=rt.engine_config)
    before_ids = {finding_identity(finding) for finding in before_case.findings}
    derived_ids = tuple(
        f"{finding.kind.value}:{finding.entity.canonical}:{','.join(finding.evidence_ids)}"
        for finding in case.findings
        if finding_identity(finding) not in before_ids
    )
    ledger = state.get("ledger", ())
    native_observation = state.get("pending_observation")
    native_records = (
        records_from_observation(native_observation)
        if native_observation is not None and native_observation.error is None
        else ()
    )
    updated_observations = state["observations"]
    if ledger and (derived_ids or native_records):
        latest = ledger[-1]
        hypothesis_ids = tuple(
            sorted(
                hypothesis.hypothesis_id
                for hypothesis in case.hypotheses
                if latest.target == hypothesis.causal_actor or latest.target in hypothesis.members
            )
        )
        returned_refs = set(latest.returned_evidence_refs)
        aligned_existing_finding = any(
            returned_refs.intersection(finding.evidence_ids)
            and (finding.entity == latest.target or latest.target in finding.related)
            for finding in case.findings
        )
        outcome = (
            GapOutcomeKind.SUPPORTS
            if (derived_ids or aligned_existing_finding) and hypothesis_ids
            else latest.outcome
        )
        ledger = (
            *ledger[:-1],
            latest.model_copy(
                update={
                    "normalized_finding_ids": tuple(
                        dict.fromkeys((*latest.normalized_finding_ids, *derived_ids))
                    ),
                    "affected_hypothesis_ids": hypothesis_ids or latest.affected_hypothesis_ids,
                    "outcome": outcome if native_records else latest.outcome,
                }
            ),
        )
        if native_records:
            updated_observations = tuple(
                item.model_copy(
                    update={
                        "hypothesis_ids": hypothesis_ids,
                        "outcome": outcome,
                    }
                )
                if item.observation_id == latest.query_id
                else item
                for item in updated_observations
            )
    return {
        "current_diagnosis": diagnosis,
        "investigation_findings": combined,
        "ledger": ledger,
        "observations": updated_observations,
        "trace_steps": _with_step(state, "rebuild", f"resolution={diagnosis.resolution.value}"),
    }


def _route_after_rebuild(state: InvestigationState) -> str:
    return "check_progress"


def _check_progress(state: InvestigationState, rt: _Runtime) -> dict[str, Any]:
    current = state["current_diagnosis"]
    current_resolution = current.resolution
    current_gap_fingerprint = _gap_fingerprint(current)
    current_case = rt.case_for(
        state.get("acquired_evidence_refs", ()), state["investigation_findings"]
    )
    current_evidence_fingerprint = _evidence_fingerprint(current_case)
    current_world_model_fingerprint = world_model_fingerprint(current_case)
    current_hypothesis_fingerprint = _hypothesis_fingerprint(current)
    previous = state.get("previous_resolution", state["initial_diagnosis"].resolution)
    no_progress = state["no_progress_count"]
    unchanged = (
        current_resolution is previous
        and current_gap_fingerprint == _frozen(state["previous_gap_fingerprint"])
        and current_evidence_fingerprint == _frozen(state["previous_evidence_fingerprint"])
        and current_hypothesis_fingerprint == _frozen(state["previous_hypothesis_fingerprint"])
        and current_world_model_fingerprint == state["previous_world_model_fingerprint"]
        and state.get("last_new_raw_evidence_count", 0) == 0
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
        "previous_world_model_fingerprint": current_world_model_fingerprint,
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
    rebuild_case: Callable[..., Case] | None = None,
    backend: InvestigationBackend | None = None,
    evidence_store: InMemoryEvidenceStore | None = None,
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
        evidence_store=evidence_store,
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
    rebuild_case: Callable[..., Case] | None = None,
    evidence_store: InMemoryEvidenceStore | None = None,
) -> InvestigationResult:
    """Run one isolated bounded investigation and return deterministic output."""
    initial_source = initial_view(source)
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
        evidence_store=evidence_store,
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
        "acquired_evidence_refs": (),
        "attempted_actions": (),
        "attempted_observations": (),
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
        "previous_world_model_fingerprint": world_model_fingerprint(case),
        "frontier_queried_dimensions": (),
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
    "world_model_fingerprint",
]
