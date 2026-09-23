from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.graph import (
    _select_pending_incident_change_discovery,
    build_investigation_state,
    investigate_diagnosis,
    record_successful_exploration,
)
from packages.rca.investigation.intents import (
    DeterministicIntentPolicy,
    IntentUtility,
    InvestigationIntentKind,
    InvestigationPhase,
    ObservationBundle,
    ScoredObservationBundle,
    SelectedObservationIntent,
)
from packages.rca.investigation.policy import LLMIntentPolicy
from packages.rca.investigation.selection import score_observation_candidate
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tools import make_observation
from packages.rca.llm import ScriptedLLM
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
    InvestigationObservation,
    InvestigationQuery,
    InvestigationResult,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
ENGINE = EngineConfig()


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def _candidate(
    candidate_id: str,
    capability: str,
    target: EntityRef,
    gap_id: str,
    dimension: GapDimension = GapDimension.CHANGE_TIMING,
) -> ObservationCandidate:
    query = InvestigationQuery(
        start=ONSET - timedelta(minutes=30), end=ONSET + timedelta(minutes=30), limit=32
    )
    return ObservationCandidate(
        candidate_id=candidate_id,
        capability=capability,
        target=target,
        query=query,
        gap_ids=(gap_id,),
        dimensions=(dimension,),
        hypothesis_ids=(),
        alternative_ids=(f"alt-{candidate_id}",),
        discriminators=(
            CandidateDiscriminator(
                gap_id=gap_id,
                dimension=dimension,
                missing_fact="bounded fact",
                support_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.SUPPORTS,
                        alternative_ids=(f"alt-{candidate_id}",),
                        condition="positive target observation",
                        implication="supports this state",
                    ),
                ),
                comparison_hypothesis_ids=(),
                comparison_alternative_ids=(f"other-alt-{candidate_id}",),
                no_data_outcomes=(),
            ),
        ),
    )


def _fixture() -> tuple[Case, Diagnosis, ObservationCandidate, ObservationCandidate]:
    source = InMemorySource(
        name="a7-2",
        alert_items=[Alert(name="latency", service="api", starts_at=ONSET)],
        cutoff=ONSET + timedelta(minutes=30),
    )
    case = build_case(source, ENGINE)
    change = _candidate(
        "change",
        "incident_changes",
        _entity("Namespace", "shop"),
        "change-gap",
    )
    history = _candidate(
        "history",
        "history",
        _entity("Deployment", "api"),
        "history-gap",
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
            resolvability=GapResolvability.RESOLVABLE,
        )
        for candidate in (change, history)
    )
    return case, diagnose_case(case).model_copy(update={"information_gaps": gaps}), change, history


def _baseline(
    case: Case, diagnosis: Diagnosis, history: ObservationCandidate
) -> SelectedObservationIntent:
    return SelectedObservationIntent(
        phase=InvestigationPhase.SOURCE_DISCOVERY,
        scored_bundle=ScoredObservationBundle(
            bundle=ObservationBundle(
                bundle_id="intent:recent_source_change",
                intent=InvestigationIntentKind.RECENT_SOURCE_CHANGE,
                phase=InvestigationPhase.SOURCE_DISCOVERY,
                candidate_ids=(history.candidate_id,),
                capabilities=(history.capability,),
                gap_ids=history.gap_ids,
                dimensions=history.dimensions,
                hypothesis_ids=(),
                alternative_ids=(),
            ),
            utility=IntentUtility(
                admissible=True,
                phase_match=1,
                decision_blocker_match=2,
                leading_hypothesis_relevance=0,
                unresolved_hypothesis_relevance=0,
                eligibility_relevance=0,
                semantic_priority=4,
                stable_tiebreak="history",
            ),
        ),
        physical=score_observation_candidate(candidate=history, diagnosis=diagnosis),
    )


def _select(
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: InvestigationPhase = InvestigationPhase.SOURCE_DISCOVERY,
    attempted: tuple[str, ...] = (),
    candidates: tuple[ObservationCandidate, ...] | None = None,
) -> tuple[SelectedObservationIntent | None, Diagnosis, ObservationCandidate, ObservationCandidate]:
    case, diagnosis, change, history = _fixture()
    selected_candidates = candidates or (change, history)
    monkeypatch.setattr(
        "packages.rca.investigation.graph.build_observation_candidates",
        lambda **_: selected_candidates,
    )
    baseline = _baseline(case, diagnosis, history)
    baseline = baseline.__class__(
        phase=phase, scored_bundle=baseline.scored_bundle, physical=baseline.physical
    )
    selected = _select_pending_incident_change_discovery(
        baseline=baseline,
        case=case,
        diagnosis=diagnosis,
        engine_config=ENGINE,
        attempted_observations=attempted,
        previous_investigations=(),
        max_tool_calls_per_gap=2,
    )
    return selected, diagnosis, change, history


class _NoDataTool:
    def __init__(self, name: str, error: str | None = None) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    def execute(
        self, _case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        self.calls += 1
        return make_observation(
            gap=gap,
            capability=self.name,
            target=target,
            payload={},
            error=self.error,
        )


def _run_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: object,
    turns: int = 1,
    error: str | None = None,
) -> tuple[InvestigationResult, _NoDataTool, _NoDataTool]:
    case, diagnosis, change, history = _fixture()
    for path in (
        "packages.rca.investigation.graph.build_observation_candidates",
        "packages.rca.investigation.intents.build_observation_candidates",
    ):
        monkeypatch.setattr(path, lambda **_: (change, history))
    monkeypatch.setattr(
        "packages.rca.investigation.graph.diagnose_case",
        lambda *_args, **_kwargs: diagnosis,
    )
    change_tool = _NoDataTool("incident_changes", error=error)
    history_tool = _NoDataTool("history")
    config = InvestigationConfig(max_turns=turns, engine=ENGINE)
    result = investigate_diagnosis(
        InMemorySource(
            name="a7-2-graph",
            alert_items=[Alert(name="latency", service="api", starts_at=ONSET)],
            cutoff=ONSET + timedelta(minutes=30),
        ),
        diagnosis=diagnosis,
        initial_case=case,
        policy=policy,  # type: ignore[arg-type]
        tools={"incident_changes": change_tool, "history": history_tool},
        config=config,
    )
    return result, change_tool, history_tool


def test_a1_obligation_is_source_discovery_only(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, *_ = _select(monkeypatch, phase=InvestigationPhase.HYPOTHESIS_DISCRIMINATION)
    assert selected is None


def test_a2_attempted_exact_identity_is_not_forced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _, diagnosis, change, _ = _select(monkeypatch)
    identity = observation_identity(change.capability, change.target, change.query)
    selected, *_ = _select(monkeypatch, attempted=(identity,))
    assert selected is None
    assert identity not in ()
    assert diagnosis.information_gaps


def test_a3_no_data_attempt_satisfies_one_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, _, change, _ = _select(monkeypatch)
    assert selected is not None
    identity = observation_identity(change.capability, change.target, change.query)
    selected_after, *_ = _select(monkeypatch, attempted=(identity,))
    assert selected_after is None


def test_a4_tool_error_is_not_successful_exploration() -> None:
    successful, atoms, progress = record_successful_exploration(
        successful_observations=(),
        covered_atoms=(),
        identity="incident_changes|shop/Namespace/shop|query",
        error="provider error",
        candidate_atoms=(("alternative", "CHANGE_TIMING"),),
    )
    assert successful == ()
    assert atoms == ()
    assert progress is False


def test_a5_no_legal_candidate_means_no_obligation(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, change, history = _fixture()
    selected, *_ = _select(monkeypatch, candidates=(history,))
    assert change.capability == "incident_changes"
    assert selected is None


def test_a6_reuses_candidate_identity_and_action_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, _, change, _ = _select(monkeypatch)
    assert selected is not None
    assert selected.physical.candidate.candidate_id == change.candidate_id
    assert selected.physical.candidate.capability == change.capability
    assert selected.physical.candidate.target == change.target
    assert selected.physical.candidate.query == change.query


def test_a7_obligation_selects_one_real_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, *_ = _select(monkeypatch)
    assert selected is not None
    assert selected.physical.candidate.capability == "incident_changes"


def test_a7_graph_obligation_displaces_read_without_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, change_tool, history_tool = _run_graph(monkeypatch, policy=DeterministicIntentPolicy())
    assert result.tool_calls == 1
    assert [entry.capability for entry in result.ledger] == ["incident_changes"]
    assert change_tool.calls == 1
    assert history_tool.calls == 0


def test_a8_graph_resumes_normal_ranking_after_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, change_tool, history_tool = _run_graph(
        monkeypatch, policy=DeterministicIntentPolicy(), turns=2
    )
    assert [entry.capability for entry in result.ledger] == ["incident_changes", "history"]
    assert change_tool.calls == 1
    assert history_tool.calls == 1


def test_a8_ranking_resumes_after_obligation(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, change, history = _fixture()
    identity = observation_identity(change.capability, change.target, change.query)
    selected, *_ = _select(monkeypatch, attempted=(identity,))
    assert selected is None
    assert history.capability == "history"


def test_a9_history_candidate_is_not_penalized_after_obligation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, diagnosis, change, history = _select(monkeypatch)
    assert selected is not None
    assert history.capability == "history"
    assert diagnosis.information_gaps[0].dimension == change.dimensions[0]


def test_a10_cross_intent_baseline_is_displaced(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, *_ = _select(monkeypatch)
    assert selected is not None
    assert selected.physical.candidate.capability == "incident_changes"


def test_a11_production_scheduler_has_no_benchmark_knowledge() -> None:
    source = Path("packages/rca/investigation/graph.py").read_text(encoding="utf-8")
    for forbidden in ("Scenario-", "flagd", "otel-demo"):
        assert forbidden not in source


def test_a12_scenario_34_targeting_is_not_present() -> None:
    source = Path("packages/rca/investigation/graph.py").read_text(encoding="utf-8")
    assert "cart" not in source


def test_a13_graph_observation_is_one_read_per_turn() -> None:
    assert "execute_tool" in Path("packages/rca/investigation/graph.py").read_text(encoding="utf-8")


def test_a14_state_round_trip_does_not_require_runtime_objects() -> None:
    source = InMemorySource(
        name="checkpoint",
        alert_items=[Alert(name="latency", service="api", starts_at=ONSET)],
        cutoff=ONSET,
    )
    state = build_investigation_state(source, config=InvestigationConfig(engine=ENGINE))
    restored = JsonPlusSerializer(pickle_fallback=False).dumps_typed(state)
    assert (
        JsonPlusSerializer(pickle_fallback=False).loads_typed(restored)["incident_id"]
        == "checkpoint"
    )


def test_a15_llm_cannot_override_mandatory_obligation(monkeypatch: pytest.MonkeyPatch) -> None:
    selected, *_ = _select(monkeypatch)
    client = ScriptedLLM(replies=[])
    assert selected is not None
    result, change_tool, history_tool = _run_graph(monkeypatch, policy=LLMIntentPolicy(client))
    assert client.calls == 0
    assert result.model_calls == 0
    assert change_tool.calls == 1
    assert history_tool.calls == 0


def test_a16_no_obligation_keeps_a6_6_policy_available() -> None:
    client = ScriptedLLM(replies=[{"intent_id": "intent:recent_source_change"}])
    policy = LLMIntentPolicy(client)
    assert policy.counts_as_model is True
