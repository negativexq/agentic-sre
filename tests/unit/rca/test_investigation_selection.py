from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import (
    CandidateDiscriminator,
    ObservationCandidate,
    build_observation_candidates,
)
from packages.rca.investigation.selection import (
    candidate_to_action,
    rank_observation_candidates,
    select_observation_candidate,
)
from packages.rca.model import (
    Alert,
    Diagnosis,
    EntityRef,
    FrontierStatus,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationLedgerEntry,
    InvestigationQuery,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _case() -> Case:
    return build_case(
        InMemorySource(
            name="selection",
            alert_items=[Alert(name="latency", service="payment", starts_at=ONSET)],
            cutoff=ONSET + timedelta(minutes=30),
        )
    )


def _gap(
    gap_id: str,
    dimension: GapDimension,
    *,
    hypothesis_ids: tuple[str, ...] = (),
    alternative_ids: tuple[str, ...] = (),
    capability: str = "history",
    target: EntityRef | None = None,
) -> InformationGap:
    from packages.rca.model import AuthorizedQuery

    target = target or _entity("Deployment", gap_id)
    candidate_alternative_ids = alternative_ids or (f"alt-{gap_id}",)
    support_hypothesis_ids = hypothesis_ids
    support_alternative_ids = candidate_alternative_ids if not hypothesis_ids else ()
    return InformationGap(
        gap_id=gap_id,
        dimension=dimension,
        hypothesis_ids=hypothesis_ids,
        alternative_ids=candidate_alternative_ids,
        missing_fact=gap_id,
        discriminating_outcomes=(
            GapOutcome(
                kind=GapOutcomeKind.SUPPORTS,
                hypothesis_ids=support_hypothesis_ids,
                alternative_ids=support_alternative_ids,
                condition=f"the missing fact supports {gap_id}",
                implication="positive evidence may distinguish the target state",
            ),
            GapOutcome(
                kind=GapOutcomeKind.NO_DATA,
                hypothesis_ids=hypothesis_ids,
                alternative_ids=candidate_alternative_ids,
                condition="the source returns no observation",
                implication="no state is contradicted",
            ),
        ),
        authorized_queries=(
            AuthorizedQuery(
                capability=capability,
                target=target,
                alternative_ids=candidate_alternative_ids,
            ),
        ),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _diagnosis(
    case: Case,
    gaps: tuple[InformationGap, ...],
    *,
    trace: ResolutionTrace | None = None,
    alternatives: tuple[StructuralAlternative, ...] = (),
) -> Diagnosis:
    diagnosis = diagnose_case(case)
    explicit = {item.alternative_id: item for item in alternatives}
    for gap in gaps:
        for alternative_id in gap.alternative_ids:
            explicit.setdefault(
                alternative_id,
                StructuralAlternative(
                    alternative_id=alternative_id,
                    actor=_entity("Deployment", alternative_id),
                    role="candidate",
                ),
            )
    if len(explicit) < 2:
        explicit.setdefault(
            "alt-peer",
            StructuralAlternative(
                alternative_id="alt-peer",
                actor=_entity("Deployment", "alt-peer"),
                role="candidate",
            ),
        )
    return diagnosis.model_copy(
        update={
            "information_gaps": gaps,
            "resolution_trace": trace,
            "structural_alternatives": tuple(explicit.values()),
        }
    )


def _candidate(
    candidate_id: str,
    *,
    capability: str = "history",
    target: EntityRef | None = None,
    gap_ids: tuple[str, ...] = ("gap",),
    dimensions: tuple[GapDimension, ...] = (GapDimension.CHANGE_TIMING,),
    hypothesis_ids: tuple[str, ...] = (),
    alternative_ids: tuple[str, ...] = (),
    start: datetime = ONSET - timedelta(minutes=30),
    end: datetime = ONSET + timedelta(minutes=30),
) -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=candidate_id,
        capability=capability,
        target=target or _entity("Deployment", candidate_id),
        query=InvestigationQuery(start=start, end=end, limit=32),
        gap_ids=gap_ids,
        dimensions=dimensions,
        hypothesis_ids=hypothesis_ids,
        alternative_ids=alternative_ids,
        discriminators=tuple(
            CandidateDiscriminator(
                gap_id=gap_id,
                dimension=dimensions[0],
                missing_fact=gap_id,
                support_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.SUPPORTS,
                        hypothesis_ids=hypothesis_ids or (f"h-{candidate_id}",),
                        alternative_ids=alternative_ids,
                        condition=f"positive evidence for {candidate_id}",
                        implication="distinguishes this candidate state",
                    ),
                ),
                comparison_hypothesis_ids=(f"h-other-{candidate_id}",),
                comparison_alternative_ids=(f"alt-other-{candidate_id}",),
                no_data_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.NO_DATA,
                        condition="no observation",
                        implication="does not discriminate",
                    ),
                ),
            )
            for gap_id in gap_ids
        ),
    )


def test_leading_hypothesis_beats_generic_breadth() -> None:
    case = _case()
    leading = _gap(
        "leading",
        GapDimension.CHANGE_TIMING,
        hypothesis_ids=("h-leading",),
    )
    broad = tuple(_gap(f"broad-{index}", GapDimension.CONFIG_DIFFERENCE) for index in range(5))
    diagnosis = _diagnosis(
        case,
        (leading, *broad),
        trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=("h-leading",),
            unresolved_hypotheses=("h-leading",),
        ),
    )
    candidates = (
        _candidate("leading-candidate", gap_ids=("leading",), hypothesis_ids=("h-leading",)),
        _candidate("broad-candidate", gap_ids=tuple(gap.gap_id for gap in broad)),
    )
    ranked = rank_observation_candidates(candidates=candidates, diagnosis=diagnosis)
    assert ranked[0].candidate.candidate_id == "leading-candidate"
    assert ranked[0].utility.hypothesis_relevance == 3


def test_unresolved_structural_alternative_beats_irrelevant_candidate() -> None:
    case = _case()
    alternative = StructuralAlternative(
        alternative_id="alt-open",
        actor=_entity("ConfigMap", "payment-config"),
        role="configuration_source",
        status=FrontierStatus.UNEXPLORED,
    )
    open_gap = _gap(
        "open-gap",
        GapDimension.CHANGE_TIMING,
        alternative_ids=("alt-open",),
    )
    irrelevant = _gap("irrelevant", GapDimension.CHANGE_TIMING)
    diagnosis = _diagnosis(case, (open_gap, irrelevant), alternatives=(alternative,))
    ranked = rank_observation_candidates(
        candidates=(
            _candidate("open", gap_ids=("open-gap",), alternative_ids=("alt-open",)),
            _candidate("irrelevant", gap_ids=("irrelevant",)),
        ),
        diagnosis=diagnosis,
    )
    assert ranked[0].candidate.candidate_id == "open"
    assert ranked[0].utility.structural_relevance == 2


def test_shared_coverage_is_a_later_tiebreak() -> None:
    case = _case()
    gaps = tuple(_gap(f"g-{index}", GapDimension.CHANGE_TIMING) for index in range(4))
    diagnosis = _diagnosis(case, gaps)
    ranked = rank_observation_candidates(
        candidates=(
            _candidate("four", gap_ids=tuple(gap.gap_id for gap in gaps)),
            _candidate("one", gap_ids=("g-0",)),
        ),
        diagnosis=diagnosis,
    )
    assert ranked[0].candidate.candidate_id == "four"


def test_seed_hidden_overlap_beats_history_only_when_relevance_ties() -> None:
    case = _case()
    history_gap = _gap("history", GapDimension.CHANGE_TIMING)
    trace_gap = _gap(
        "trace",
        GapDimension.DEPENDENCY_HEALTH,
        capability="runtime_traces",
        target=_entity("Deployment", "payment"),
    )
    diagnosis = _diagnosis(case, (history_gap, trace_gap))
    ranked = rank_observation_candidates(
        candidates=(
            _candidate("history-candidate", gap_ids=("history",)),
            _candidate(
                "trace-candidate",
                capability="runtime_traces",
                target=_entity("Deployment", "payment"),
                gap_ids=("trace",),
            ),
        ),
        diagnosis=diagnosis,
    )
    assert ranked[0].candidate.candidate_id == "trace-candidate"


def test_cost_tier_breaks_equal_overlap_without_causal_relevance() -> None:
    case = _case()
    event_gap = _gap("event", GapDimension.EVENT_SEQUENCE, capability="events")
    log_gap = _gap("log", GapDimension.LOG_ERROR_PATTERN, capability="logs")
    diagnosis = _diagnosis(case, (event_gap, log_gap))
    ranked = rank_observation_candidates(
        candidates=(
            _candidate("logs", capability="logs", gap_ids=("log",)),
            _candidate("events", capability="events", gap_ids=("event",)),
        ),
        diagnosis=diagnosis,
    )
    assert ranked[0].candidate.candidate_id == "events"


def test_attempted_physical_observation_is_removed() -> None:
    case = _case()
    candidate = _candidate("attempted")
    gap = _gap("gap", GapDimension.CHANGE_TIMING)
    identity = observation_identity(candidate.capability, candidate.target, candidate.query)
    diagnosis = _diagnosis(case, (gap,))
    assert (
        rank_observation_candidates(
            candidates=(candidate,),
            diagnosis=diagnosis,
            attempted_observations=(identity,),
        )
        == ()
    )


def test_representative_gap_is_deterministic_and_action_query_is_explicit() -> None:
    case = _case()
    gaps = (
        _gap("gap-z", GapDimension.CHANGE_TIMING),
        _gap("gap-a", GapDimension.CONFIG_DIFFERENCE, hypothesis_ids=("h",)),
        _gap("gap-b", GapDimension.EVENT_SEQUENCE),
    )
    diagnosis = _diagnosis(
        case,
        gaps,
        trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=("h",),
            unresolved_hypotheses=("h",),
        ),
    )
    selected = select_observation_candidate(
        case=case,
        diagnosis=diagnosis,
        engine_config=EngineConfig(),
    )
    assert selected is not None
    action = candidate_to_action(selected, diagnosis)
    assert action is not None
    assert action.capability is not None
    assert action.target is not None
    assert action.gap_id == "gap-a"
    assert action.query == selected.candidate.query
    assert action.query is not None
    assert observation_identity(action.capability, action.target, action.query) == (
        observation_identity(
            selected.candidate.capability,
            selected.candidate.target,
            selected.candidate.query,
        )
    )


def test_representative_gap_skips_an_exhausted_gap() -> None:
    case = _case()
    gaps = (
        _gap("gap-a", GapDimension.CHANGE_TIMING),
        _gap("gap-b", GapDimension.CONFIG_DIFFERENCE),
    )
    diagnosis = _diagnosis(case, gaps)
    candidate = _candidate("shared", gap_ids=("gap-a", "gap-b"))
    scored = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)[0]
    exhausted = tuple(
        InvestigationLedgerEntry(
            query_id=f"old-{index}",
            gap_id="gap-a",
            capability="history",
            target=candidate.target,
        )
        for index in range(2)
    )
    action = candidate_to_action(
        scored,
        diagnosis,
        previous_investigations=exhausted,
        max_tool_calls_per_gap=2,
    )
    assert action is not None
    assert action.gap_id == "gap-b"


def test_different_query_window_is_a_different_physical_observation() -> None:
    case = _case()
    gap = _gap("gap", GapDimension.CHANGE_TIMING)
    diagnosis = _diagnosis(case, (gap,))
    first = _candidate("first", start=ONSET - timedelta(minutes=30))
    second = _candidate("second", start=ONSET - timedelta(hours=2), end=ONSET)
    first_id = observation_identity(first.capability, first.target, first.query)
    second_id = observation_identity(second.capability, second.target, second.query)
    assert first_id != second_id
    assert (
        rank_observation_candidates(
            candidates=(first, second),
            diagnosis=diagnosis,
            attempted_observations=(first_id,),
        )[0].candidate.candidate_id
        == "second"
    )


def test_deterministic_policy_replans_after_a_real_observation() -> None:
    from packages.rca.demo import demo_source
    from packages.rca.investigation.graph import investigate_diagnosis
    from packages.rca.investigation.selection import DeterministicObservationPolicy

    result = investigate_diagnosis(
        demo_source(),
        policy=DeterministicObservationPolicy(),
    )
    assert result.model_calls == 0
    assert result.tool_calls > 0
    assert len(result.ledger) == result.tool_calls
    assert len({entry.query_id for entry in result.ledger}) == result.tool_calls
    assert tuple(entry.capability for entry in result.ledger) == (
        "events",
        "resource_pressure",
    )


def test_candidate_selection_does_not_call_hidden_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    source = case.source

    def fail() -> NoReturn:
        raise AssertionError("candidate selection read hidden telemetry")

    monkeypatch.setattr(source, "object_history", fail)
    monkeypatch.setattr(source, "events", fail)
    monkeypatch.setattr(source, "error_logs", fail)
    monkeypatch.setattr(source, "resource_pressure", fail)
    monkeypatch.setattr(source, "traffic_observations", fail)
    monkeypatch.setattr(source, "trace_observations", fail)
    gap = _gap("gap", GapDimension.CHANGE_TIMING)
    diagnosis = _diagnosis(case, (gap,))
    selected = select_observation_candidate(
        case=case,
        diagnosis=diagnosis,
        engine_config=EngineConfig(),
    )
    assert selected is not None


def test_candidate_carries_explicit_positive_discriminator_and_neutral_no_data() -> None:
    case = _case()
    gap = _gap(
        "alt-a-gap",
        GapDimension.CHANGE_TIMING,
        alternative_ids=("alt-a",),
    ).model_copy(
        update={
            "discriminating_outcomes": (
                GapOutcome(
                    kind=GapOutcomeKind.SUPPORTS,
                    alternative_ids=("alt-a",),
                    condition="pre-onset change is observed for alt-a",
                    implication="supports alt-a",
                ),
                GapOutcome(
                    kind=GapOutcomeKind.NO_DATA,
                    alternative_ids=("alt-a", "alt-b"),
                    condition="the source returns no observation",
                    implication="ambiguity remains",
                ),
            )
        }
    )
    alternatives = tuple(
        StructuralAlternative(
            alternative_id=alternative_id,
            actor=_entity("Deployment", alternative_id),
            role="candidate",
            status=FrontierStatus.UNEXPLORED,
        )
        for alternative_id in ("alt-a", "alt-b")
    )
    diagnosis = _diagnosis(case, (gap,), alternatives=alternatives)

    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )

    assert len(candidates) == 1
    assert len(candidates[0].discriminators) == 1
    discriminator = candidates[0].discriminators[0]
    assert discriminator.gap_id == gap.gap_id
    assert discriminator.dimension is GapDimension.CHANGE_TIMING
    assert discriminator.support_outcomes[0].alternative_ids == ("alt-a",)
    assert discriminator.comparison_alternative_ids == ("alt-b",)
    assert len(discriminator.no_data_outcomes) == 1
    assert discriminator.no_data_outcomes[0].kind is GapOutcomeKind.NO_DATA


def test_candidate_does_not_claim_discrimination_from_no_data_alone() -> None:
    case = _case()
    gap = _gap(
        "alt-a-gap",
        GapDimension.CHANGE_TIMING,
        alternative_ids=("alt-a",),
    ).model_copy(
        update={
            "discriminating_outcomes": (
                GapOutcome(
                    kind=GapOutcomeKind.NO_DATA,
                    alternative_ids=("alt-a", "alt-b"),
                    condition="the source returns no observation",
                    implication="ambiguity remains",
                ),
            )
        }
    )
    alternatives = tuple(
        StructuralAlternative(
            alternative_id=alternative_id,
            actor=_entity("Deployment", alternative_id),
            role="candidate",
            status=FrontierStatus.UNEXPLORED,
        )
        for alternative_id in ("alt-a", "alt-b")
    )
    diagnosis = _diagnosis(case, (gap,), alternatives=alternatives)

    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )

    assert len(candidates) == 1
    assert candidates[0].discriminators == ()


def test_ranked_action_names_positive_discriminator_and_keeps_no_data_neutral() -> None:
    case = _case()
    gaps = tuple(
        _gap(
            f"gap-{alternative_id}",
            GapDimension.CHANGE_TIMING,
            alternative_ids=(alternative_id,),
        )
        for alternative_id in ("alt-a", "alt-b")
    )
    alternatives = tuple(
        StructuralAlternative(
            alternative_id=alternative_id,
            actor=_entity("Deployment", alternative_id),
            role="candidate",
        )
        for alternative_id in ("alt-a", "alt-b")
    )
    diagnosis = _diagnosis(case, gaps, alternatives=alternatives)
    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )
    blind_candidate = candidates[0].__class__(**{**candidates[0].__dict__, "discriminators": ()})
    ranked = rank_observation_candidates(
        candidates=(blind_candidate, *candidates), diagnosis=diagnosis
    )

    assert ranked[0].utility.discriminating_gap_coverage > 0
    action = candidate_to_action(ranked[0], diagnosis)
    assert action is not None
    assert f"discriminator gap={action.gap_id}" in action.rationale
    assert "NO_DATA/UNKNOWN are non-discriminating" in action.rationale
