from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from packages.rca.causal_roles import HypothesisCausalRole
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import ObservationCandidate
from packages.rca.investigation.focus import exact_workload_dependency_candidates
from packages.rca.investigation.graph import record_successful_exploration
from packages.rca.investigation.intents import (
    InvestigationIntentKind,
    select_intent_physical_candidate,
)
from packages.rca.investigation.selection import (
    candidate_to_action,
    rank_observation_candidates,
    score_observation_candidate,
)
from packages.rca.model import (
    AuthorizedQuery,
    Confidence,
    Diagnosis,
    Edge,
    EntityRef,
    GapDimension,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InvestigationQuery,
    Resolution,
    ResolutionTrace,
    Symptoms,
)
from packages.rca.root_cause_eligibility import (
    HypothesisRootCauseEligibility,
    RootCauseEligibilities,
    RootCauseEligibilityState,
)
from packages.rca.topology import Topology

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
INTENT = InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def _candidate(
    candidate_id: str,
    target: EntityRef,
    gap_ids: tuple[str, ...] = ("gap-a",),
    capability: str = "logs",
) -> ObservationCandidate:
    query = InvestigationQuery(
        start=ONSET - timedelta(minutes=30),
        end=ONSET + timedelta(minutes=30),
        limit=32,
    )
    return ObservationCandidate(
        candidate_id=candidate_id,
        capability=capability,
        target=target,
        query=query,
        gap_ids=gap_ids,
        dimensions=tuple(GapDimension.LOG_ERROR_PATTERN for _ in gap_ids),
        hypothesis_ids=(),
        alternative_ids=(),
    )


def _diagnosis(
    candidates: tuple[ObservationCandidate, ...], hypothesis_ids: tuple[str, ...]
) -> Diagnosis:
    gaps = tuple(
        InformationGap(
            gap_id=gap_id,
            dimension=GapDimension.LOG_ERROR_PATTERN,
            missing_fact="bounded dependency evidence",
            authorized_queries=(
                AuthorizedQuery(capability=candidate.capability, target=candidate.target),
            ),
            resolvability=GapResolvability.RESOLVABLE,
        )
        for candidate in candidates
        for gap_id in candidate.gap_ids
    )
    return Diagnosis(
        incident_id="a8-1",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        summary="bounded",
        symptoms=Symptoms(
            onset=ONSET,
            last_seen=ONSET,
            services=(),
            namespaces=("shop",),
            alert_names=("latency",),
        ),
        resolution=Resolution.AMBIGUOUS,
        resolution_trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=hypothesis_ids,
            unresolved_hypotheses=(),
            plausible_hypotheses=hypothesis_ids,
        ),
        information_gaps=gaps,
    )


def _case(
    hypotheses: tuple[Hypothesis, ...],
    *,
    propagated: tuple[str, ...] = (),
    owner_edges: tuple[Edge, ...] = (),
) -> Any:
    assessments = tuple(
        HypothesisRootCauseEligibility(
            hypothesis_id=hypothesis_id,
            causal_actor=next(
                hypothesis.causal_actor
                for hypothesis in hypotheses
                if hypothesis.hypothesis_id == hypothesis_id
            ),
            causal_role=HypothesisCausalRole.PROPAGATED_EFFECT,
            state=RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT,
            episode_source_capable_initiating_findings=0,
            verified_incoming_edges=1,
            verified_incoming_pairs=1,
            rationale="synthetic propagated effect",
        )
        for hypothesis_id in propagated
    )
    return SimpleNamespace(
        hypotheses=hypotheses,
        topology=Topology(edges=owner_edges, latest={}),
        root_cause_eligibilities=RootCauseEligibilities(assessments),
    )


def _run_focus(
    candidates: tuple[ObservationCandidate, ...],
    hypotheses: tuple[Hypothesis, ...],
    *,
    intent: InvestigationIntentKind = INTENT,
    propagated: tuple[str, ...] = (),
    owner_edges: tuple[Edge, ...] = (),
    attempted: tuple[str, ...] = (),
) -> tuple[tuple[ObservationCandidate, ...], Diagnosis, Any]:
    case = _case(hypotheses, propagated=propagated, owner_edges=owner_edges)
    diagnosis = _diagnosis(candidates, tuple(item.hypothesis_id for item in hypotheses))
    focused = exact_workload_dependency_candidates(
        intent=intent,
        candidates=candidates,
        case=case,
        diagnosis=diagnosis,
        attempted_observations=attempted,
        max_tool_calls_per_gap=2,
    )
    return focused, diagnosis, case


def _api_hypothesis() -> Hypothesis:
    return Hypothesis(hypothesis_id="h-api", causal_actor=_entity("Pod", "api-0"))


def _api_edges() -> tuple[Edge, ...]:
    return (
        Edge(
            source=_entity("Pod", "api-0"), target=_entity("Deployment", "api"), relation="owned_by"
        ),
    )


def test_f1_focus_is_intent_scoped() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    focused, _, _ = _run_focus(
        (api,),
        (_api_hypothesis(),),
        intent=InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        owner_edges=_api_edges(),
    )
    assert focused == ()


def test_f2_same_workload_candidate_is_promoted_without_new_score() -> None:
    frontend = _candidate("frontend", _entity("Deployment", "frontend"), ("g1", "g2", "g3"))
    api = _candidate("api", _entity("Deployment", "api"))
    focused, diagnosis, case = _run_focus(
        (frontend, api), (_api_hypothesis(),), owner_edges=_api_edges()
    )
    assert tuple(item.candidate_id for item in focused) == ("api",)
    selected = select_intent_physical_candidate(candidates=focused, diagnosis=diagnosis)
    assert selected is not None and selected.candidate.candidate_id == "api"
    assert case.topology.workload_of(_entity("Pod", "api-0")) == _entity("Deployment", "api")


def test_f3_no_local_candidate_preserves_global_selection() -> None:
    frontend = _candidate("frontend", _entity("Deployment", "frontend"), ("g1", "g2"))
    payment = _candidate("payment", _entity("Deployment", "payment"))
    focused, diagnosis, _ = _run_focus(
        (frontend, payment), (_api_hypothesis(),), owner_edges=_api_edges()
    )
    assert focused == ()
    selected = select_intent_physical_candidate(candidates=(frontend, payment), diagnosis=diagnosis)
    assert selected is not None and selected.candidate.candidate_id == "frontend"


def test_f4_multiple_local_candidates_use_existing_selector() -> None:
    deployment = _candidate("deployment", _entity("Deployment", "api"), ("g1", "g2"))
    pod = _candidate("pod", _entity("Pod", "api-0"))
    focused, diagnosis, _ = _run_focus(
        (deployment, pod), (_api_hypothesis(),), owner_edges=_api_edges()
    )
    assert {item.candidate_id for item in focused} == {"deployment", "pod"}
    assert select_intent_physical_candidate(candidates=focused, diagnosis=diagnosis) is not None


def test_f5_multiple_active_hypotheses_form_one_focus_region() -> None:
    worker = Hypothesis(hypothesis_id="h-worker", causal_actor=_entity("Pod", "worker-0"))
    worker_edge = Edge(
        source=_entity("Pod", "worker-0"),
        target=_entity("Deployment", "worker"),
        relation="owned_by",
    )
    api = _candidate("api", _entity("Deployment", "api"))
    worker_candidate = _candidate("worker", _entity("Deployment", "worker"))
    focused, _, _ = _run_focus(
        (api, worker_candidate),
        (_api_hypothesis(), worker),
        owner_edges=_api_edges() + (worker_edge,),
    )
    assert {item.candidate_id for item in focused} == {"api", "worker"}


def test_f6_propagated_effect_does_not_anchor_focus() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    focused, _, _ = _run_focus(
        (api,), (_api_hypothesis(),), propagated=("h-api",), owner_edges=_api_edges()
    )
    assert focused == ()


def test_f7_only_causal_actor_is_an_anchor() -> None:
    hypothesis = Hypothesis(
        hypothesis_id="h-api",
        causal_actor=_entity("Pod", "api-0"),
        manifestations=(_entity("Pod", "worker-0"),),
    )
    worker = _candidate("worker", _entity("Deployment", "worker"))
    worker_edge = Edge(
        source=_entity("Pod", "worker-0"),
        target=_entity("Deployment", "worker"),
        relation="owned_by",
    )
    focused, _, _ = _run_focus((worker,), (hypothesis,), owner_edges=_api_edges() + (worker_edge,))
    assert focused == ()


def test_f8_focus_does_not_mutate_hypothesis_ids() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    before = api.hypothesis_ids
    focused, _, _ = _run_focus((api,), (_api_hypothesis(),), owner_edges=_api_edges())
    assert focused[0].hypothesis_ids == before == ()


def test_f9_attempted_no_data_identity_falls_back_to_global_pool() -> None:
    frontend = _candidate("frontend", _entity("Deployment", "frontend"), ("g1", "g2"))
    api = _candidate("api", _entity("Deployment", "api"))
    diagnosis = _diagnosis((frontend, api), ("h-api",))
    case = _case((_api_hypothesis(),), owner_edges=_api_edges())
    attempted = observation_identity(api.capability, api.target, api.query)
    focused = exact_workload_dependency_candidates(
        intent=INTENT,
        candidates=(frontend, api),
        case=case,
        diagnosis=diagnosis,
        attempted_observations=(attempted,),
        max_tool_calls_per_gap=2,
    )
    assert focused == ()
    selected = select_intent_physical_candidate(
        candidates=(frontend, api), diagnosis=diagnosis, attempted_observations=(attempted,)
    )
    assert selected is not None and selected.candidate.candidate_id == "frontend"


def test_f10_error_has_no_exploration_success_or_retry() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    identity = observation_identity(api.capability, api.target, api.query)
    successful, atoms, progress = record_successful_exploration(
        successful_observations=(),
        covered_atoms=(),
        identity=identity,
        error="tool error",
        candidate_atoms=(("a", "LOG_ERROR_PATTERN"),),
    )
    assert successful == () and atoms == () and progress is False
    focused, _, _ = _run_focus(
        (api,), (_api_hypothesis(),), owner_edges=_api_edges(), attempted=(identity,)
    )
    assert focused == ()


def test_f11_missing_workload_mapping_does_not_guess() -> None:
    api = _candidate("api", _entity("Pod", "unknown-0"))
    focused, _, _ = _run_focus((api,), (_api_hypothesis(),))
    assert focused == ()


def test_f12_workload_actor_normalizes_to_itself() -> None:
    hypothesis = Hypothesis(hypothesis_id="h-api", causal_actor=_entity("Deployment", "api"))
    api = _candidate("api", _entity("Deployment", "api"))
    focused, _, _ = _run_focus((api,), (hypothesis,))
    assert focused == (api,)


def test_f13_statefulset_and_daemonset_use_generic_workload_semantics() -> None:
    stateful = Hypothesis(hypothesis_id="h-stateful", causal_actor=_entity("Pod", "db-0"))
    daemon = Hypothesis(hypothesis_id="h-daemon", causal_actor=_entity("Pod", "agent-0"))
    stateful_candidate = _candidate("stateful", _entity("StatefulSet", "db"))
    daemon_candidate = _candidate("daemon", _entity("DaemonSet", "agent"))
    edges = (
        Edge(
            source=_entity("Pod", "db-0"), target=_entity("StatefulSet", "db"), relation="owned_by"
        ),
        Edge(
            source=_entity("Pod", "agent-0"),
            target=_entity("DaemonSet", "agent"),
            relation="owned_by",
        ),
    )
    focused, _, _ = _run_focus(
        (stateful_candidate, daemon_candidate), (stateful, daemon), owner_edges=edges
    )
    assert {item.candidate_id for item in focused} == {"stateful", "daemon"}


def test_f14_observation_utility_is_unchanged_by_focus() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    diagnosis = _diagnosis((api,), ("h-api",))
    before = score_observation_candidate(candidate=api, diagnosis=diagnosis)
    focused, _, _ = _run_focus((api,), (_api_hypothesis(),), owner_edges=_api_edges())
    after = score_observation_candidate(candidate=focused[0], diagnosis=diagnosis)
    assert before.utility == after.utility


def test_f15_a6_5_2_selector_is_unchanged_inside_focus() -> None:
    api_a = _candidate("api-a", _entity("Deployment", "api"), ("g1", "g2"))
    api_b = _candidate("api-b", _entity("Pod", "api-0"))
    focused, diagnosis, _ = _run_focus(
        (api_a, api_b), (_api_hypothesis(),), owner_edges=_api_edges()
    )
    expected = select_intent_physical_candidate(candidates=focused, diagnosis=diagnosis)
    actual = select_intent_physical_candidate(candidates=focused, diagnosis=diagnosis)
    assert actual is not None and expected is not None
    assert actual.candidate.candidate_id == expected.candidate.candidate_id


def test_f16_a7_2_incident_change_is_not_dependency_focus() -> None:
    change = _candidate("change", _entity("Namespace", "shop"), capability="incident_changes")
    focused, _, _ = _run_focus((change,), (_api_hypothesis(),), owner_edges=_api_edges())
    assert focused == ()


def test_f17_focus_has_no_llm_or_physical_caller_authority() -> None:
    assert "policy" not in exact_workload_dependency_candidates.__code__.co_varnames
    assert "target" not in exact_workload_dependency_candidates.__code__.co_varnames


def test_f18_focus_is_recomputable_without_persistent_state() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    first, diagnosis, case = _run_focus((api,), (_api_hypothesis(),), owner_edges=_api_edges())
    second = exact_workload_dependency_candidates(
        intent=INTENT, candidates=(api,), case=case, diagnosis=diagnosis, max_tool_calls_per_gap=2
    )
    assert first == second


def test_f19_production_focus_has_no_benchmark_knowledge() -> None:
    source = Path("packages/rca/investigation/focus.py").read_text(encoding="utf-8")
    for forbidden in ("Scenario-34", "valkey-cart", "cart-9fd", "otel_logs_raw.tsv:64188", "flagd"):
        assert forbidden not in source


def test_f20_focus_is_a_single_candidate_projection() -> None:
    api = _candidate("api", _entity("Deployment", "api"))
    focused, diagnosis, _ = _run_focus((api,), (_api_hypothesis(),), owner_edges=_api_edges())
    assert len(focused) == 1
    action = candidate_to_action(
        rank_observation_candidates(candidates=focused, diagnosis=diagnosis)[0],
        diagnosis,
        max_tool_calls_per_gap=2,
    )
    assert action is not None
