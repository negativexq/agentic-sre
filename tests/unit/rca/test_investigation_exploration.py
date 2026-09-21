from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.investigation.candidates import ObservationCandidate
from packages.rca.investigation.graph import (
    _check_progress,
    _Runtime,
    build_investigation_state,
    record_successful_exploration,
    world_model_fingerprint,
)
from packages.rca.investigation.intents import select_intent_physical_candidate
from packages.rca.investigation.selection import exploration_coverage_atoms
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationQuery,
    ResolutionTrace,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _entity(name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind="Deployment", name=name)


def _case() -> Case:
    return build_case(
        InMemorySource(
            name="exploration",
            alert_items=[Alert(name="latency", service="caller", starts_at=ONSET)],
            cutoff=ONSET + timedelta(minutes=30),
        )
    )


def _gap(
    gap_id: str,
    *,
    alternatives: tuple[str, ...] = (),
    dimension: GapDimension = GapDimension.DEPENDENCY_HEALTH,
) -> InformationGap:
    target = _entity(gap_id)
    return InformationGap(
        gap_id=gap_id,
        dimension=dimension,
        alternative_ids=alternatives,
        missing_fact=gap_id,
        authorized_queries=(
            AuthorizedQuery(
                capability="logs",
                target=target,
            ),
        ),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _candidate(
    candidate_id: str,
    gap_ids: tuple[str, ...],
    *,
    alternative_ids: tuple[str, ...] = (),
    hypothesis_ids: tuple[str, ...] = (),
) -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=candidate_id,
        capability="logs",
        target=_entity(candidate_id),
        query=InvestigationQuery(start=ONSET, end=ONSET, limit=32),
        gap_ids=gap_ids,
        dimensions=(GapDimension.DEPENDENCY_HEALTH,),
        hypothesis_ids=hypothesis_ids,
        alternative_ids=alternative_ids,
    )


def _diagnosis(case: Case, gaps: tuple[InformationGap, ...], *, leading: bool = False) -> Diagnosis:
    trace = (
        ResolutionTrace(
            state=diagnose_case(case).resolution,
            leading_hypothesis_ids=("h-leading",),
        )
        if leading
        else None
    )
    return diagnose_case(case).model_copy(
        update={"information_gaps": gaps, "resolution_trace": trace}
    )


def test_successful_no_data_is_novel_exploration_progress() -> None:
    first = record_successful_exploration(
        successful_observations=(), covered_atoms=(), identity="logs|A|q", error=None
    )
    second = record_successful_exploration(
        successful_observations=first[0],
        covered_atoms=first[1],
        identity="logs|B|q",
        error=None,
    )
    assert first[2] is True
    assert second[2] is True
    assert second[0] == ("logs|A|q", "logs|B|q")


def test_repeated_identity_is_not_novel_exploration() -> None:
    result = record_successful_exploration(
        successful_observations=("logs|A|q",),
        covered_atoms=(("alt-a", "DEPENDENCY_HEALTH"),),
        identity="logs|A|q",
        error=None,
        candidate_atoms=(("alt-b", "DEPENDENCY_HEALTH"),),
    )
    assert result[2] is False
    assert result[1] == (("alt-a", "DEPENDENCY_HEALTH"),)


def test_tool_error_is_not_exploration_progress_or_coverage() -> None:
    result = record_successful_exploration(
        successful_observations=(),
        covered_atoms=(),
        identity="logs|A|q",
        error="provider failed",
        candidate_atoms=(("alt-a", "DEPENDENCY_HEALTH"),),
    )
    assert result == ((), (), False)


def test_exploration_bookkeeping_does_not_mutate_rca_fingerprint() -> None:
    case = _case()
    before = world_model_fingerprint(case)
    record_successful_exploration(
        successful_observations=(),
        covered_atoms=(),
        identity="logs|A|q",
        error=None,
        candidate_atoms=(("alt-a", "DEPENDENCY_HEALTH"),),
    )
    assert world_model_fingerprint(case) == before
    assert diagnose_case(case).model_dump(mode="json") == diagnose_case(case).model_dump(
        mode="json"
    )


def test_stronger_a6_2_relevance_beats_more_uncovered_atoms() -> None:
    case = _case()
    gaps = (
        _gap("a-gap", alternatives=("alt-a",)),
        _gap("b-gap-1", alternatives=("alt-b1",)),
        _gap("b-gap-2", alternatives=("alt-b2",)),
        _gap("b-gap-3", alternatives=("alt-b3",)),
    )
    diagnosis = _diagnosis(case, gaps, leading=True)
    a = _candidate("candidate-a", ("a-gap",), hypothesis_ids=("h-leading",))
    b = _candidate(
        "candidate-b",
        ("b-gap-1", "b-gap-2", "b-gap-3"),
        alternative_ids=("alt-b1", "alt-b2", "alt-b3"),
    )
    selected = select_intent_physical_candidate(candidates=(a, b), diagnosis=diagnosis)
    assert selected is not None
    assert selected.candidate.candidate_id == "candidate-a"


def test_pre_read_equivalent_candidates_use_marginal_coverage() -> None:
    case = _case()
    gaps = (
        _gap("a-1", alternatives=("alt-1",), dimension=GapDimension.DEPENDENCY_HEALTH),
        _gap("a-2", alternatives=("alt-2",), dimension=GapDimension.LOG_ERROR_PATTERN),
        _gap("b-1", alternatives=("alt-1", "alt-2"), dimension=GapDimension.DEPENDENCY_HEALTH),
        _gap("b-2", alternatives=("alt-1", "alt-2"), dimension=GapDimension.LOG_ERROR_PATTERN),
    )
    diagnosis = _diagnosis(case, gaps)
    a = _candidate("candidate-z", ("a-1", "a-2"), alternative_ids=("alt-1", "alt-2"))
    b = _candidate("candidate-a", ("b-1", "b-2"), alternative_ids=("alt-1", "alt-2"))
    selected = select_intent_physical_candidate(candidates=(a, b), diagnosis=diagnosis)
    assert selected is not None
    assert len(exploration_coverage_atoms(b, diagnosis)) > len(
        exploration_coverage_atoms(a, diagnosis)
    )
    assert selected.candidate.candidate_id == "candidate-a"


def test_candidate_id_is_final_exploration_tie_break() -> None:
    case = _case()
    gap = _gap("same-gap", alternatives=("alt",))
    diagnosis = _diagnosis(case, (gap,))
    a = _candidate("candidate-a", ("same-gap",), alternative_ids=("alt",))
    b = _candidate("candidate-b", ("same-gap",), alternative_ids=("alt",))
    selected = select_intent_physical_candidate(candidates=(b, a), diagnosis=diagnosis)
    assert selected is not None
    assert selected.candidate.candidate_id == "candidate-a"


def test_one_graph_turn_has_one_physical_observation() -> None:
    from packages.rca.demo import demo_source
    from packages.rca.investigation.graph import investigate_diagnosis
    from packages.rca.investigation.intents import DeterministicIntentPolicy

    result = investigate_diagnosis(
        demo_source(), policy=DeterministicIntentPolicy(), config=InvestigationConfig(max_turns=1)
    )
    assert result.tool_calls <= result.turns
    assert result.tool_calls == len(result.ledger)


def test_gap_frontier_change_prevents_false_stagnation() -> None:
    source = InMemorySource(name="gap-change")
    case = build_case(source)
    state = build_investigation_state(source, initial_case=case)
    state["previous_gap_fingerprint"] = (("different",),)

    class _Policy:
        counts_as_model = False

        def choose_action(self, _context: object) -> InvestigationAction:
            raise AssertionError("policy must not be called")

    runtime = _Runtime(
        source=source,
        policy=_Policy(),
        tools={},
        config=InvestigationConfig(max_no_progress_rounds=1),
        initial_case=case,
        rebuild_case=None,
        backend=None,
        evidence_store=None,
    )
    result = _check_progress(state, runtime)
    assert result["no_progress_count"] == 0


def test_exploration_state_round_trips_without_pickle() -> None:
    source = InMemorySource(name="checkpoint-exploration")
    state = build_investigation_state(source)
    state["successful_exploration_observations"] = ("logs|A|q",)
    state["exploration_covered_atoms"] = (("alt-a", "DEPENDENCY_HEALTH"),)
    encoded = JsonPlusSerializer(pickle_fallback=False).dumps_typed(state)
    restored = JsonPlusSerializer(pickle_fallback=False).loads_typed(encoded)
    assert tuple(restored["successful_exploration_observations"]) == ("logs|A|q",)
    assert tuple(tuple(item) for item in restored["exploration_covered_atoms"]) == (
        ("alt-a", "DEPENDENCY_HEALTH"),
    )
