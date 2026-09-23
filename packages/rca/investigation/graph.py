"""Low-level LangGraph orchestration for bounded evidence acquisition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
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
from packages.rca.investigation.candidates import ObservationCandidate, build_observation_candidates
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
from packages.rca.investigation.focus import exact_workload_dependency_candidates
from packages.rca.investigation.intents import (
    DeterministicIntentPolicy,
    SelectedObservationIntent,
    build_intent_menu,
    build_observation_bundles,
    derive_investigation_phase,
    intent_relevance_key,
    rank_observation_bundles,
    select_intent_physical_candidate,
    select_observation_intent_candidate,
)
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    finding_identity,
    new_investigation_findings,
    normalize_observation,
)
from packages.rca.investigation.policy import LLMIntentPolicy
from packages.rca.investigation.selection import (
    DeterministicObservationPolicy,
    candidate_to_action,
    exploration_coverage_atoms,
    rank_observation_candidates,
    score_observation_candidate,
    select_observation_candidate,
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
    EntityRef,
    Finding,
    GapDimension,
    GapOutcomeKind,
    GapResolvability,
    HypothesisEpistemicState,
    InformationGap,
    InvestigationActionAudit,
    InvestigationActionStatus,
    InvestigationExecutionStatus,
    InvestigationGapState,
    InvestigationHypothesisState,
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
            "incident_events",
            "incident_changes",
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


def record_successful_exploration(
    *,
    successful_observations: Sequence[str],
    covered_atoms: Sequence[tuple[str, str]],
    identity: str,
    error: str | None,
    candidate_atoms: Sequence[tuple[str, str]] = (),
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], bool]:
    """Record one novel successful exact read without touching RCA state."""
    successful = tuple(successful_observations)
    covered: set[tuple[str, str]] = {(atom[0], atom[1]) for atom in covered_atoms}
    if error is not None or identity in successful:
        return successful, tuple(sorted(covered)), False
    successful = (*successful, identity)
    covered.update(candidate_atoms)
    return successful, tuple(sorted(covered)), True


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


def _selected_intent_for_candidate(
    *,
    candidate: ObservationCandidate,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
    attempted_observations: Sequence[str],
    previous_investigations: Sequence[InvestigationLedgerEntry],
    max_tool_calls_per_gap: int,
) -> SelectedObservationIntent | None:
    """Bind one existing candidate back to its ranked semantic bundle.

    The discovery obligation is an ordering constraint over the already-built
    candidate surface.  It must not manufacture a target or bypass the
    existing semantic bundle/action contracts.
    """
    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=engine_config
    )
    phase = derive_investigation_phase(case, diagnosis)
    bundles = build_observation_bundles(
        case=case,
        diagnosis=diagnosis,
        candidates=candidates,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    ranked_bundles = rank_observation_bundles(
        bundles=bundles,
        phase=phase,
        diagnosis=diagnosis,
        case=case,
        source_actor_bundle_available=any(
            bundle.intent.value == "INCIDENT_ACTOR_DISCOVERY" for bundle in bundles
        ),
    )
    matching_bundle = next(
        (
            scored
            for scored in ranked_bundles
            if candidate.candidate_id in scored.bundle.candidate_ids
        ),
        None,
    )
    if matching_bundle is None:
        return None
    scored_candidate = score_observation_candidate(
        candidate=candidate,
        diagnosis=diagnosis,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    if (
        candidate_to_action(
            scored_candidate,
            diagnosis,
            previous_investigations=previous_investigations,
            max_tool_calls_per_gap=max_tool_calls_per_gap,
        )
        is None
    ):
        return None
    return SelectedObservationIntent(
        phase=phase,
        scored_bundle=matching_bundle,
        physical=scored_candidate,
    )


def _select_pending_incident_change_discovery(
    *,
    baseline: SelectedObservationIntent | None,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
    attempted_observations: Sequence[str],
    previous_investigations: Sequence[InvestigationLedgerEntry],
    max_tool_calls_per_gap: int,
) -> SelectedObservationIntent | None:
    """Return the one-shot namespace change discovery override, if required.

    This is deliberately a scheduling boundary.  The normal semantic and
    physical selectors run first; an unattempted, legal namespace
    ``incident_changes`` candidate only displaces a normal non-discovery read
    while SOURCE_DISCOVERY is active.  Candidate ranking remains the sole
    deterministic ordering inside the discovery surface.
    """
    if baseline is None:
        return None
    if baseline.phase.value != "SOURCE_DISCOVERY":
        return None
    if baseline.physical.candidate.capability in {"incident_events", "incident_changes"}:
        return None

    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=engine_config
    )
    pending = tuple(
        candidate
        for candidate in candidates
        if candidate.capability == "incident_changes" and candidate.target.kind == "Namespace"
    )
    if not pending:
        return None

    ranked = rank_observation_candidates(
        candidates=pending,
        diagnosis=diagnosis,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    for scored in ranked:
        if (
            candidate_to_action(
                scored,
                diagnosis,
                previous_investigations=previous_investigations,
                max_tool_calls_per_gap=max_tool_calls_per_gap,
            )
            is None
        ):
            continue
        selected = _selected_intent_for_candidate(
            candidate=scored.candidate,
            case=case,
            diagnosis=diagnosis,
            engine_config=engine_config,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
            max_tool_calls_per_gap=max_tool_calls_per_gap,
        )
        if selected is not None:
            return selected
    return None


def _focused_physical_candidates(
    *,
    selected_intent: SelectedObservationIntent,
    candidates: Sequence[ObservationCandidate],
    case: Case,
    diagnosis: Diagnosis,
    attempted_observations: Sequence[str],
    previous_investigations: Sequence[InvestigationLedgerEntry],
    max_tool_calls_per_gap: int,
) -> tuple[ObservationCandidate, ...]:
    """Apply exact-workload focus after semantic intent selection only."""
    return exact_workload_dependency_candidates(
        intent=selected_intent.scored_bundle.bundle.intent,
        candidates=candidates,
        case=case,
        diagnosis=diagnosis,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
        max_tool_calls_per_gap=max_tool_calls_per_gap,
    )


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
    case = rt.case_for(state.get("acquired_evidence_refs", ()), state["investigation_findings"])
    baseline_intent = None
    if isinstance(rt.policy, (DeterministicIntentPolicy, LLMIntentPolicy)):
        baseline_intent = select_observation_intent_candidate(
            case=case,
            diagnosis=diagnosis,
            engine_config=rt.engine_config,
            attempted_observations=state.get("attempted_observations", ()),
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
            exploration_covered_atoms=state.get("exploration_covered_atoms", ()),
        )
        obligated = _select_pending_incident_change_discovery(
            baseline=baseline_intent,
            case=case,
            diagnosis=diagnosis,
            engine_config=rt.engine_config,
            attempted_observations=state.get("attempted_observations", ()),
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
        )
        if obligated is not None:
            action = candidate_to_action(
                obligated.physical,
                diagnosis,
                previous_investigations=tuple(state.get("ledger", ())),
                max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
            )
            if action is not None:
                bundle = obligated.scored_bundle.bundle
                return {
                    "pending_action": action,
                    "pending_intent_id": bundle.bundle_id,
                    "pending_intent_kind": bundle.intent.value,
                    "stop_reason": None,
                    "turns": state["turns"] + 1,
                    "model_calls": state["model_calls"],
                    "trace_steps": _with_step(
                        state,
                        "select_action",
                        f"{action.action} {action.capability or ''} {action.target or ''}: "
                        f"phase={obligated.phase.value}; intent={bundle.intent.value}; "
                        "selection=discovery-obligation; "
                        f"candidate={obligated.physical.candidate.candidate_id}",
                    ),
                }
    if isinstance(rt.policy, LLMIntentPolicy):
        candidates = build_observation_candidates(
            case=case, diagnosis=diagnosis, engine_config=rt.engine_config
        )
        phase = derive_investigation_phase(case, diagnosis)
        bundles = build_observation_bundles(
            case=case,
            diagnosis=diagnosis,
            candidates=candidates,
            attempted_observations=state.get("attempted_observations", ()),
            previous_investigations=tuple(state.get("ledger", ())),
        )
        ranked_bundles = rank_observation_bundles(
            bundles=bundles,
            phase=phase,
            diagnosis=diagnosis,
            case=case,
            source_actor_bundle_available=any(
                bundle.intent.value == "INCIDENT_ACTOR_DISCOVERY" for bundle in bundles
            ),
        )
        top_relevance = intent_relevance_key(ranked_bundles[0]) if ranked_bundles else None
        top_bundles = tuple(
            scored
            for scored in ranked_bundles
            if top_relevance is not None and intent_relevance_key(scored) == top_relevance
        )
        menu = build_intent_menu(top_bundles)
        if not menu:
            return {
                "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
                "turns": state["turns"] + 1,
                "model_calls": state["model_calls"],
                "trace_steps": _with_step(state, "select_action", "no admissible intent menu"),
            }
        selected_id = top_bundles[0].bundle.bundle_id
        selection_source = "deterministic-top-intent"
        fallback_reason: str | None = None
        model_calls = state["model_calls"]
        if len(menu) > 1:
            before = _policy_calls(rt.policy)
            try:
                returned_id = rt.policy.choose_intent(
                    menu, tuple(state.get("intent_history", ())[-6:])
                )
                if returned_id in {item.intent_id for item in menu}:
                    selected_id = returned_id
                    selection_source = "Luna-top-tiebreak"
                else:
                    fallback_reason = "unknown intent_id"
            except LLMError as error:
                fallback_reason = f"model failure: {type(error).__name__}"
            model_calls += max(0, _policy_calls(rt.policy) - before)
            if fallback_reason is not None:
                selection_source = "deterministic-top-fallback"
        selected_bundle = next(
            scored for scored in top_bundles if scored.bundle.bundle_id == selected_id
        )
        allowed = tuple(
            candidate
            for candidate in candidates
            if candidate.candidate_id in selected_bundle.bundle.candidate_ids
        )
        focused = exact_workload_dependency_candidates(
            intent=selected_bundle.bundle.intent,
            candidates=allowed,
            case=case,
            diagnosis=diagnosis,
            attempted_observations=state.get("attempted_observations", ()),
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
        )
        physical_pool = focused or allowed
        selected = select_intent_physical_candidate(
            candidates=physical_pool,
            diagnosis=diagnosis,
            attempted_observations=state.get("attempted_observations", ()),
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
            exploration_covered_atoms=state.get("exploration_covered_atoms", ()),
        )
        if selected is None:
            return {
                "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
                "turns": state["turns"] + 1,
                "model_calls": model_calls,
                "trace_steps": _with_step(
                    state, "select_action", "selected intent had no executable candidate"
                ),
            }
        action = candidate_to_action(
            selected,
            diagnosis,
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
        )
        if action is None:
            return {
                "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
                "turns": state["turns"] + 1,
                "model_calls": model_calls,
                "trace_steps": _with_step(
                    state, "select_action", "selected candidate had no executable gap"
                ),
            }
        intent_utility = selected_bundle.utility
        detail = (
            f"{action.action} {action.capability or ''} {action.target or ''}: "
            f"phase={phase.value}; intent={selected_bundle.bundle.intent.value}; "
            f"selection={selection_source}; menu={len(menu)}; "
            f"bundle-size={len(selected_bundle.bundle.candidate_ids)}; "
            f"blocker={intent_utility.decision_blocker_match}; "
            f"leading={intent_utility.leading_hypothesis_relevance}; "
            f"unresolved={intent_utility.unresolved_hypothesis_relevance}; "
            f"candidate={selected.candidate.candidate_id}"
        )
        if focused:
            detail += "; physical-focus=EXACT_SAME_WORKLOAD"
        if fallback_reason is not None:
            detail += f"; fallback={fallback_reason}"
        return {
            "pending_action": action,
            "pending_intent_id": selected_id,
            "pending_intent_kind": selected_bundle.bundle.intent.value,
            "stop_reason": None,
            "turns": state["turns"] + 1,
            "model_calls": model_calls,
            "trace_steps": _with_step(state, "select_action", detail),
        }
    if isinstance(rt.policy, (DeterministicObservationPolicy, DeterministicIntentPolicy)):
        selected_intent = None
        focus_applied = False
        if isinstance(rt.policy, DeterministicIntentPolicy):
            selected_intent = baseline_intent
            selected = selected_intent.physical if selected_intent is not None else None
            if selected_intent is not None:
                candidates = build_observation_candidates(
                    case=case, diagnosis=diagnosis, engine_config=rt.engine_config
                )
                allowed = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.candidate_id in selected_intent.scored_bundle.bundle.candidate_ids
                )
                focused = _focused_physical_candidates(
                    selected_intent=selected_intent,
                    candidates=allowed,
                    case=case,
                    diagnosis=diagnosis,
                    attempted_observations=state.get("attempted_observations", ()),
                    previous_investigations=tuple(state.get("ledger", ())),
                    max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
                )
                if focused:
                    focused_selected = select_intent_physical_candidate(
                        candidates=focused,
                        diagnosis=diagnosis,
                        attempted_observations=state.get("attempted_observations", ()),
                        previous_investigations=tuple(state.get("ledger", ())),
                        max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
                        exploration_covered_atoms=state.get("exploration_covered_atoms", ()),
                    )
                    if focused_selected is not None:
                        selected_intent = SelectedObservationIntent(
                            phase=selected_intent.phase,
                            scored_bundle=selected_intent.scored_bundle,
                            physical=focused_selected,
                        )
                        selected = focused_selected
                        focus_applied = True
        else:
            selected = select_observation_candidate(
                case=case,
                diagnosis=diagnosis,
                engine_config=rt.engine_config,
                attempted_observations=state.get("attempted_observations", ()),
                previous_investigations=tuple(state.get("ledger", ())),
                max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
            )
        if selected is None:
            return {
                "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
                "turns": state["turns"] + 1,
                "model_calls": state["model_calls"],
                "trace_steps": _with_step(
                    state,
                    "select_action",
                    "deterministic selector found no admissible physical candidate",
                ),
            }
        action = candidate_to_action(
            selected,
            diagnosis,
            previous_investigations=tuple(state.get("ledger", ())),
            max_tool_calls_per_gap=rt.config.max_tool_calls_per_gap,
        )
        if action is None:
            return {
                "stop_reason": InvestigationStopReason.NO_RESOLVABLE_GAP,
                "turns": state["turns"] + 1,
                "model_calls": state["model_calls"],
                "trace_steps": _with_step(
                    state,
                    "select_action",
                    "deterministic candidate had no executable representative gap",
                ),
            }
        if selected_intent is not None:
            bundle_utility = selected_intent.scored_bundle.utility
            bundle = selected_intent.scored_bundle.bundle
            covered_atoms = {
                (atom[0], atom[1]) for atom in state.get("exploration_covered_atoms", ())
            }
            detail = (
                f"{action.action} {action.capability or ''} {action.target or ''}: "
                f"phase={selected_intent.phase.value}; intent={bundle.intent.value}; "
                f"bundle-size={len(bundle.candidate_ids)}; "
                f"blocker={bundle_utility.decision_blocker_match}; "
                f"leading={bundle_utility.leading_hypothesis_relevance}; "
                f"unresolved={bundle_utility.unresolved_hypothesis_relevance}; "
                f"marginal-coverage={len(exploration_coverage_atoms(selected.candidate, diagnosis) - covered_atoms)}; "
                f"candidate={selected.candidate.candidate_id}"
            )
            if focus_applied:
                detail += "; physical-focus=EXACT_SAME_WORKLOAD"
        else:
            utility = selected.utility
            detail = (
                f"{action.action} {action.capability or ''} {action.target or ''}: "
                f"{action.rationale}; hypothesis={utility.hypothesis_relevance}; "
                f"structural={utility.structural_relevance}; overlap={selected.overlap_class}; "
                f"cost={utility.cost_tier}; gaps={utility.discriminating_gap_coverage}; "
                f"dimensions={utility.dimension_coverage}; "
                f"shared-gaps={utility.shared_gap_coverage}; "
                f"shared-alternatives={utility.shared_alternative_coverage}"
            )
        return {
            "pending_action": action,
            "pending_intent_id": (
                selected_intent.scored_bundle.bundle.bundle_id
                if selected_intent is not None
                else None
            ),
            "pending_intent_kind": (
                selected_intent.scored_bundle.bundle.intent.value
                if selected_intent is not None
                else None
            ),
            "stop_reason": None,
            "turns": state["turns"] + 1,
            "model_calls": state["model_calls"],
            "trace_steps": _with_step(state, "select_action", detail),
        }
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


def _hypothesis_states(diagnosis: Diagnosis) -> tuple[InvestigationHypothesisState, ...]:
    """Snapshot deterministic hypothesis epistemic states without inference."""
    audited = (
        {
            item.hypothesis_id: item.epistemic_state
            for item in diagnosis.resolution_trace.hypothesis_audits
        }
        if diagnosis.resolution_trace is not None
        else {}
    )
    hypotheses = (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *(diagnosis.alternative_hypotheses or diagnosis.ambiguous_hypotheses),
    )
    unique: dict[str, InvestigationHypothesisState] = {}
    for hypothesis in hypotheses:
        unique[hypothesis.hypothesis_id] = InvestigationHypothesisState(
            hypothesis_id=hypothesis.hypothesis_id,
            actor=hypothesis.causal_actor,
            state=audited.get(hypothesis.hypothesis_id, HypothesisEpistemicState.UNRESOLVED),
        )
    return tuple(unique[key] for key in sorted(unique))


def _gap_states(diagnosis: Diagnosis) -> tuple[InvestigationGapState, ...]:
    return tuple(
        InvestigationGapState(
            gap_id=gap.gap_id,
            dimension=gap.dimension,
            missing_fact=gap.missing_fact,
            resolvability=gap.resolvability,
        )
        for gap in sorted(diagnosis.information_gaps, key=lambda item: item.gap_id)
    )


def _leading_actor(diagnosis: Diagnosis) -> EntityRef | None:
    if diagnosis.hypothesis is not None:
        return diagnosis.hypothesis.causal_actor
    if diagnosis.root_cause is not None:
        return diagnosis.root_cause
    if diagnosis.alternative_hypotheses:
        return diagnosis.alternative_hypotheses[0].causal_actor
    if diagnosis.ambiguous_hypotheses:
        return diagnosis.ambiguous_hypotheses[0].causal_actor
    return None


def _append_action_audit(
    state: InvestigationState, audit: InvestigationActionAudit
) -> tuple[InvestigationActionAudit, ...]:
    return (*state.get("action_audits", ()), audit)


def _update_latest_action_audit(
    state: InvestigationState, **changes: Any
) -> tuple[InvestigationActionAudit, ...]:
    audits = state.get("action_audits", ())
    if not audits:
        return ()
    return (*audits[:-1], audits[-1].model_copy(update=changes))


def _audit_before_state(diagnosis: Diagnosis) -> dict[str, Any]:
    return {
        "resolution_before": diagnosis.resolution,
        "leading_actor_before": _leading_actor(diagnosis),
        "hypothesis_states_before": _hypothesis_states(diagnosis),
        "gap_states_before": _gap_states(diagnosis),
    }


def _audit_after_state(audit: InvestigationActionAudit, diagnosis: Diagnosis) -> dict[str, Any]:
    hypothesis_states = _hypothesis_states(diagnosis)
    gap_states = _gap_states(diagnosis)
    leading_actor = _leading_actor(diagnosis)
    changed = (
        audit.resolution_before != diagnosis.resolution
        or audit.leading_actor_before != leading_actor
        or audit.hypothesis_states_before != hypothesis_states
        or audit.gap_states_before != gap_states
    )
    return {
        "resolution_after": diagnosis.resolution,
        "leading_actor_after": leading_actor,
        "hypothesis_states_after": hypothesis_states,
        "gap_states_after": gap_states,
        "decision_state_changed": changed,
    }


def _audit_progress_classification(
    audit: InvestigationActionAudit, *, exploration_progress: bool
) -> str:
    if audit.authorization_result == "REJECTED":
        return "REJECTED"
    if audit.authorization_result == "POLICY_STOP":
        return "POLICY_STOP"
    if audit.decision_state_changed:
        return "DECISION_STATE_CHANGED"
    if audit.normalized_finding_ids:
        return "NEW_FINDING"
    if audit.new_evidence_refs:
        return "NOVEL_EVIDENCE_ONLY"
    if exploration_progress:
        return "FRONTIER_PROGRESS"
    return "NO_PROGRESS"


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
        audit = InvestigationActionAudit(
            turn_index=state["turns"],
            action=action,
            intent_id=state.get("pending_intent_id"),
            intent_kind=state.get("pending_intent_kind"),
            gap_dimension=next(
                (
                    gap.dimension
                    for gap in state["current_diagnosis"].information_gaps
                    if gap.gap_id == action.gap_id
                ),
                None,
            ),
            missing_fact=next(
                (
                    gap.missing_fact
                    for gap in state["current_diagnosis"].information_gaps
                    if gap.gap_id == action.gap_id
                ),
                None,
            ),
            authorization_result="REJECTED",
            authorization_reason=result.reason,
            resolution_after=state["current_diagnosis"].resolution,
            leading_actor_after=_leading_actor(state["current_diagnosis"]),
            hypothesis_states_after=_hypothesis_states(state["current_diagnosis"]),
            gap_states_after=_gap_states(state["current_diagnosis"]),
            decision_state_changed=False,
            progress_classification="REJECTED",
            **_audit_before_state(state["current_diagnosis"]),
        )
        return {
            "action_audits": _append_action_audit(state, audit),
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
        audit = InvestigationActionAudit(
            turn_index=state["turns"],
            action=action,
            intent_id=state.get("pending_intent_id"),
            intent_kind=state.get("pending_intent_kind"),
            authorization_result="POLICY_STOP",
            authorization_reason="policy selected stop; no backend action authorized",
            resolution_after=state["current_diagnosis"].resolution,
            leading_actor_after=_leading_actor(state["current_diagnosis"]),
            hypothesis_states_after=_hypothesis_states(state["current_diagnosis"]),
            gap_states_after=_gap_states(state["current_diagnosis"]),
            decision_state_changed=False,
            progress_classification="POLICY_STOP",
            **_audit_before_state(state["current_diagnosis"]),
        )
        return {
            "action_audits": _append_action_audit(state, audit),
            "stop_reason": InvestigationStopReason.POLICY_STOP,
            "action_validation_status": InvestigationActionStatus.VALID_STOP,
            "trace_steps": _with_step(state, "policy_stop", action.rationale),
        }
    identity = action_identity(action)
    assert action.target is not None and action.capability is not None
    read_identity = observation_identity(action.capability, action.target, action.query)
    gap = next(
        item for item in state["current_diagnosis"].information_gaps if item.gap_id == action.gap_id
    )
    audit = InvestigationActionAudit(
        turn_index=state["turns"],
        action=action,
        intent_id=state.get("pending_intent_id"),
        intent_kind=state.get("pending_intent_kind"),
        gap_dimension=gap.dimension,
        missing_fact=gap.missing_fact,
        authorization_result="AUTHORIZED",
        authorization_reason="capability/target pair is authorized for the selected resolvable gap",
        **_audit_before_state(state["current_diagnosis"]),
    )
    return {
        "action_audits": _append_action_audit(state, audit),
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
    action_audits = _update_latest_action_audit(
        state,
        backend_execution_status=(
            InvestigationExecutionStatus.FAILED
            if observation.error is not None
            else InvestigationExecutionStatus.SUCCEEDED
        ),
        observation_id=observation.observation_id,
        observation_outcome=observation.outcome,
    )
    return {
        "action_audits": action_audits,
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
        returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
        already_known_refs = tuple(ref for ref in returned_refs if ref in known_refs)
        invalid_new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
        failed_observation = observation.model_copy(
            update={
                "payload": {},
                "error": f"{type(error).__name__}: {error}",
            }
        )
        action_audits = _update_latest_action_audit(
            state,
            backend_execution_status=InvestigationExecutionStatus.FAILED,
            observation_id=failed_observation.observation_id,
            observation_outcome=failed_observation.outcome,
            returned_evidence_refs=returned_refs,
            new_evidence_refs=invalid_new_refs,
            already_known_refs=already_known_refs,
        )
        return {
            "action_audits": action_audits,
            "pending_observation": failed_observation,
            "pending_returned_evidence_refs": returned_refs,
            "pending_new_evidence_refs": invalid_new_refs,
            "pending_already_known_refs": already_known_refs,
            "last_new_raw_evidence_count": len(invalid_new_refs),
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
    action_audits = _update_latest_action_audit(
        state,
        returned_evidence_refs=returned_refs,
        new_evidence_refs=tuple(new_refs),
        already_known_refs=tuple(known),
    )
    return {
        "action_audits": action_audits,
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
        return {"last_new_evidence_count": 0, "last_exploration_progress": False}
    successful = tuple(state.get("successful_exploration_observations", ()))
    covered_atoms: tuple[tuple[str, str], ...] = tuple(
        (atom[0], atom[1]) for atom in state.get("exploration_covered_atoms", ())
    )
    candidate_atoms: tuple[tuple[str, str], ...] = ()
    action = state.get("pending_action")
    if (
        isinstance(rt.policy, (DeterministicIntentPolicy, LLMIntentPolicy))
        and observation.error is None
        and action is not None
        and action.capability is not None
    ):
        identity = observation_identity(action.capability, observation.target, action.query)
        if identity not in successful:
            newly_acquired = set(state.get("pending_new_evidence_refs", ()))
            pre_read_refs = tuple(
                ref for ref in state.get("acquired_evidence_refs", ()) if ref not in newly_acquired
            )
            current_case = rt.case_for(pre_read_refs, state["investigation_findings"])
            candidate = next(
                (
                    item
                    for item in build_observation_candidates(
                        case=current_case,
                        diagnosis=state["current_diagnosis"],
                        engine_config=rt.engine_config,
                    )
                    if observation_identity(item.capability, item.target, item.query) == identity
                ),
                None,
            )
            if candidate is not None:
                candidate_atoms = tuple(
                    exploration_coverage_atoms(candidate, state["current_diagnosis"])
                )
    successful, covered_atoms, exploration_progress = record_successful_exploration(
        successful_observations=successful,
        covered_atoms=covered_atoms,
        identity=(
            observation_identity(action.capability, observation.target, action.query)
            if action is not None and action.capability is not None
            else ""
        ),
        error=(
            observation.error
            if isinstance(rt.policy, (DeterministicIntentPolicy, LLMIntentPolicy))
            and action is not None
            and action.capability is not None
            else "disabled"
        ),
        candidate_atoms=candidate_atoms,
    )
    gap = next(
        (
            gap
            for gap in state["current_diagnosis"].information_gaps
            if gap.gap_id == observation.gap_id
        ),
        None,
    )
    if gap is None:
        action_audits = _update_latest_action_audit(
            state,
            returned_evidence_refs=state.get("pending_returned_evidence_refs", ()),
            new_evidence_refs=state.get("pending_new_evidence_refs", ()),
            already_known_refs=state.get("pending_already_known_refs", ()),
        )
        return {
            "action_audits": action_audits,
            "observations": (*state["observations"], observation),
            "last_new_evidence_count": 0,
            "successful_exploration_observations": tuple(successful),
            "exploration_covered_atoms": tuple(sorted(covered_atoms)),
            "last_exploration_progress": exploration_progress,
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
    queried_dimensions = {
        alternative_id: {GapDimension(value) for value in dimensions}
        for alternative_id, dimensions in state.get("frontier_queried_dimensions", ())
    }
    if observation.error is None and observation.capability != "runtime_traces" and new_refs:
        covered = covered_frontier_dimensions(
            state["current_diagnosis"],
            capability=observation.capability,
            target=observation.target,
            evidence_acquired=True,
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
    intent_history = (
        *state.get("intent_history", ()),
        {
            "intent_kind": state.get("pending_intent_kind") or "unknown",
            "capability": observation.capability,
            "outcome": normalized_observation.outcome.value,
            "returned_refs": len(returned_refs),
            "new_refs": len(new_refs),
            "findings": len(finding_ids),
            "decision_state_changed": False,
        },
    )[-6:]
    existing_ids = {item.observation_id for item in state["observations"]}
    observations = (
        state["observations"]
        if observation.observation_id in existing_ids
        else (*state["observations"], normalized_observation)
    )
    action_audits = _update_latest_action_audit(
        state,
        observation_id=normalized_observation.observation_id,
        observation_outcome=normalized_observation.outcome,
        returned_evidence_refs=returned_refs,
        new_evidence_refs=new_refs,
        already_known_refs=tuple(ref for ref in returned_refs if ref in known_refs),
        normalized_finding_ids=finding_ids,
        affected_hypothesis_ids=normalized_observation.hypothesis_ids,
    )
    return {
        "action_audits": action_audits,
        "observations": observations,
        "ledger": (*state.get("ledger", ()), ledger_entry),
        "intent_history": intent_history,
        "pending_findings": fresh_findings,
        "successful_exploration_observations": tuple(successful),
        "exploration_covered_atoms": tuple(sorted(covered_atoms)),
        "last_exploration_progress": exploration_progress,
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
    action_audits = state.get("action_audits", ())
    if action_audits:
        latest_audit = action_audits[-1]
        after_state = _audit_after_state(latest_audit, diagnosis)
        if ledger:
            latest_entry = ledger[-1]
            after_state.update(
                {
                    "normalized_finding_ids": tuple(
                        dict.fromkeys(
                            (
                                *latest_audit.normalized_finding_ids,
                                *latest_entry.normalized_finding_ids,
                            )
                        )
                    ),
                    "affected_hypothesis_ids": latest_entry.affected_hypothesis_ids,
                    "observation_outcome": latest_entry.outcome,
                }
            )
        action_audits = _update_latest_action_audit(state, **after_state)
    return {
        "action_audits": action_audits,
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
        and current_evidence_fingerprint == _frozen(state["previous_evidence_fingerprint"])
        and current_hypothesis_fingerprint == _frozen(state["previous_hypothesis_fingerprint"])
        and current_world_model_fingerprint == state["previous_world_model_fingerprint"]
        and state.get("last_new_raw_evidence_count", 0) == 0
    )
    gap_changed = current_gap_fingerprint != _frozen(state["previous_gap_fingerprint"])
    exploration_progress = state.get("last_exploration_progress", False)
    if unchanged and not exploration_progress and not gap_changed:
        no_progress += 1
    else:
        no_progress = 0
    intent_history = list(state.get("intent_history", ()))
    if intent_history:
        latest_history = dict(intent_history[-1])
        latest_history["decision_state_changed"] = bool(
            state.get("last_new_evidence_count", 0)
            or current_resolution is not previous
            or current_hypothesis_fingerprint != _frozen(state["previous_hypothesis_fingerprint"])
        )
        intent_history[-1] = latest_history
    action_audits = state.get("action_audits", ())
    if action_audits and action_audits[-1].authorization_result == "AUTHORIZED":
        latest_audit = action_audits[-1]
        after_state = _audit_after_state(latest_audit, current)
        updated_audit = latest_audit.model_copy(
            update={
                **after_state,
                "progress_classification": _audit_progress_classification(
                    latest_audit,
                    exploration_progress=bool(exploration_progress),
                ),
            }
        )
        action_audits = (*action_audits[:-1], updated_audit)
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
        "action_audits": action_audits,
        "previous_resolution": current.resolution,
        "previous_gap_fingerprint": current_gap_fingerprint,
        "previous_evidence_fingerprint": current_evidence_fingerprint,
        "previous_hypothesis_fingerprint": current_hypothesis_fingerprint,
        "previous_world_model_fingerprint": current_world_model_fingerprint,
        "intent_history": tuple(intent_history[-6:]),
        "no_progress_count": no_progress,
        "stop_reason": stop,
        "trace_steps": _with_step(
            state,
            "check_progress",
            f"new evidence={state['last_new_evidence_count']}; unchanged={unchanged}; "
            f"gap-changed={gap_changed}; exploration={exploration_progress}; "
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
    action_audits = state.get("action_audits", ())
    executed_audit_count = sum(
        item.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
        for item in action_audits
    )
    if executed_audit_count != state["tool_calls"]:
        raise AssertionError(
            "structured executed-action count does not match investigation tool_calls"
        )
    result = InvestigationResult(
        diagnosis=diagnosis,
        initial_diagnosis=state["initial_diagnosis"],
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
        action_audits=action_audits,
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
        "action_audits": (),
        "investigation_findings": (),
        "acquired_evidence_refs": (),
        "attempted_actions": (),
        "attempted_observations": (),
        "successful_exploration_observations": (),
        "exploration_covered_atoms": (),
        "last_exploration_progress": False,
        "intent_history": (),
        "pending_intent_id": None,
        "pending_intent_kind": None,
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
    "record_successful_exploration",
    "resume_investigation",
    "world_model_fingerprint",
]
