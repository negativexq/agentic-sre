"""Research must acquire evidence without manufacturing decision authority."""

from dataclasses import replace

import pytest

from packages.rca.engine import build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.research import research_question, research_questions
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationLedgerEntry,
    InvestigationQuery,
)
from packages.rca.source import InMemorySource


def _question(dimension: GapDimension) -> tuple[InformationGap, ObservationCandidate]:
    target = EntityRef(namespace="shop", kind="Deployment", name="peer")
    outcomes = (
        GapOutcome(
            kind=GapOutcomeKind.SUPPORTS, hypothesis_ids=("rival",), condition="positive effect"
        ),
        GapOutcome(
            kind=GapOutcomeKind.CONTRADICTS,
            hypothesis_ids=("leader",),
            condition="different origin",
        ),
    )
    gap = InformationGap(
        gap_id="gap",
        dimension=dimension,
        missing_fact="Which operation produced this failure?",
        required_relation="observed caller -> peer operation",
        hypothesis_ids=("leader", "rival"),
        entity_scope=(target,),
        evidence_refs=("log:1",),
        discriminating_outcomes=outcomes,
        authorized_queries=(AuthorizedQuery(capability="runtime_traces", target=target),),
        resolvability=GapResolvability.RESOLVABLE,
    )
    candidate = ObservationCandidate(
        candidate_id="candidate",
        capability="runtime_traces",
        target=target,
        query=InvestigationQuery(limit=8, source_log_ids=("log:1",)),
        gap_ids=(gap.gap_id,),
        dimensions=(dimension,),
        hypothesis_ids=("rival",),
        alternative_ids=(),
        discriminators=(
            CandidateDiscriminator(
                gap_id=gap.gap_id,
                dimension=dimension,
                missing_fact=gap.missing_fact,
                support_outcomes=outcomes[:1],
                comparison_hypothesis_ids=("leader",),
                comparison_alternative_ids=(),
                no_data_outcomes=(),
                expected_fact_families=("trace_call",),
            ),
        ),
    )
    return gap, candidate


@pytest.mark.parametrize("dimension", tuple(GapDimension))
def test_research_retains_scope_rival_support_and_leader_contradiction(
    dimension: GapDimension,
) -> None:
    gap, candidate = _question(dimension)
    plan = research_question(gap, (candidate,))
    assert plan.availability == "AVAILABLE"
    assert plan.missing_fact == gap.missing_fact
    assert plan.required_relation == gap.required_relation
    assert plan.entity_scope == gap.entity_scope
    assert plan.evidence_refs == gap.evidence_refs
    assert plan.possible_outcomes[:2] == gap.discriminating_outcomes
    assert {item.kind for item in plan.possible_outcomes} == set(GapOutcomeKind)
    assert plan.reads[0].query == candidate.query
    assert plan.reads[0].comparison_hypothesis_ids == ("leader",)
    assert not plan.no_data_is_exclusion
    assert not plan.resolution_authority


@pytest.mark.parametrize("mutation", ("foreign_target", "wrong_gap", "no_discriminator"))
def test_plans_cannot_authorize_unrelated_reads(mutation: str) -> None:
    gap, candidate = _question(GapDimension.DEPENDENCY_HEALTH)
    if mutation == "foreign_target":
        candidate = replace(candidate, target=EntityRef(namespace="other", kind="Pod", name="peer"))
    elif mutation == "wrong_gap":
        candidate = replace(candidate, gap_ids=("other-gap",))
    else:
        candidate = replace(candidate, discriminators=())
    plan = research_question(gap, (candidate,))
    assert plan.availability == "NO_ELIGIBLE_CANDIDATE"
    assert not plan.reads


def test_missing_registered_tool_is_unavailable_even_with_an_authorized_candidate() -> None:
    gap, candidate = _question(GapDimension.DEPENDENCY_HEALTH)
    plan = research_question(gap, (candidate,), available_capabilities=())
    assert plan.availability == "UNAVAILABLE_WITH_CURRENT_TOOLS"
    assert not plan.reads


def test_gap_budget_is_not_an_evidence_verdict() -> None:
    gap, candidate = _question(GapDimension.DEPENDENCY_HEALTH)
    entry = InvestigationLedgerEntry(
        query_id="read:1",
        gap_id=gap.gap_id,
        capability=candidate.capability,
        target=candidate.target,
        query=candidate.query,
        outcome=GapOutcomeKind.UNKNOWN,
    )
    plan = research_question(
        gap, (candidate,), previous_investigations=(entry,), max_tool_calls_per_gap=1
    )
    assert plan.availability == "GAP_BUDGET_EXHAUSTED"
    assert not plan.reads
    assert plan.possible_outcomes[:2] == gap.discriminating_outcomes
    assert not plan.no_data_is_exclusion
    assert not plan.resolution_authority


def test_unavailable_observed_and_attempted_are_different_research_limits() -> None:
    gap, candidate = _question(GapDimension.DEPENDENCY_HEALTH)
    for resolvability, expected in (
        (GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS, "UNAVAILABLE_WITH_CURRENT_TOOLS"),
        (GapResolvability.ALREADY_OBSERVED, "ALREADY_OBSERVED"),
    ):
        plan = research_question(
            gap.model_copy(update={"resolvability": resolvability}), (candidate,)
        )
        assert plan.availability == expected
        assert not plan.reads
    attempted = (observation_identity(candidate.capability, candidate.target, candidate.query),)
    plan = research_question(gap, (candidate,), attempted_observations=attempted)
    assert plan.availability == "READS_ALREADY_ATTEMPTED"
    assert not plan.reads


def test_projection_does_not_mutate_decision_or_create_new_blockers() -> None:
    gap, candidate = _question(GapDimension.DEPENDENCY_HEALTH)
    diagnosis = diagnose_case(build_case(InMemorySource(name="research"))).model_copy(
        update={"information_gaps": (gap,)}
    )
    before = diagnosis.model_dump(mode="json")
    digest = diagnosis_epistemic_digest(diagnosis)
    plans = research_questions(diagnosis, (candidate,))
    assert len(plans) == 1
    assert diagnosis.model_dump(mode="json") == before
    assert diagnosis_epistemic_digest(diagnosis) == digest
    assert research_questions(diagnosis.model_copy(update={"information_gaps": ()}), ()) == ()


def test_research_metadata_preserves_default_policy_actions_and_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import packages.rca.investigation.graph as graph
    from packages.rca.demo import demo_source
    from packages.rca.investigation.intents import DeterministicIntentPolicy

    enriched = graph.investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    assert enriched.tool_calls > 0
    assert all(
        audit.research_question is not None
        for audit in enriched.action_audits
        if audit.authorization_result == "AUTHORIZED"
    )
    monkeypatch.setattr(graph, "research_questions", lambda *args, **kwargs: ())
    monkeypatch.setattr(graph, "research_question", lambda *args, **kwargs: None)
    baseline = graph.investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    assert [audit.action for audit in enriched.action_audits] == [
        audit.action for audit in baseline.action_audits
    ]
    assert enriched.tool_calls == baseline.tool_calls
    assert enriched.ledger == baseline.ledger
    assert enriched.observations == baseline.observations
    assert enriched.stop_reason == baseline.stop_reason
    assert diagnosis_epistemic_digest(enriched.diagnosis) == diagnosis_epistemic_digest(
        baseline.diagnosis
    )
