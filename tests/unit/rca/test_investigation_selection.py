from __future__ import annotations

from dataclasses import replace
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
    active_choice_dominates_baseline,
    candidate_to_action,
    rank_observation_candidates,
    select_observation_candidate,
)
from packages.rca.model import (
    Alert,
    Diagnosis,
    EntityRef,
    Finding,
    FindingKind,
    FrontierStatus,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InformationGapOrigin,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationDiscriminatorAudit,
    InvestigationDiscriminatorKind,
    InvestigationExecutionStatus,
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


def _candidate_with_states(
    candidate_id: str,
    *,
    target: EntityRef | None = None,
    capability: str = "history",
    support: tuple[str, ...] = ("h1",),
    comparisons: tuple[str, ...] = ("h2",),
    known_fact_keys: tuple[str, ...] = (),
) -> ObservationCandidate:
    candidate = _candidate(candidate_id, capability=capability, target=target)
    discriminator = candidate.discriminators[0]
    return candidate.__class__(
        **{
            **candidate.__dict__,
            "known_fact_keys": known_fact_keys,
            "discriminators": (
                discriminator.__class__(
                    **{
                        **discriminator.__dict__,
                        "support_outcomes": (
                            GapOutcome(
                                kind=GapOutcomeKind.SUPPORTS,
                                hypothesis_ids=support,
                                condition="positive state",
                            ),
                        ),
                        "comparison_hypothesis_ids": comparisons,
                    }
                ),
            ),
        }
    )


def _prior_audit(
    candidate: ObservationCandidate,
    *,
    outcome: GapOutcomeKind = GapOutcomeKind.SUPPORTS,
) -> InvestigationActionAudit:
    discriminator = candidate.discriminators[0]
    return InvestigationActionAudit(
        turn_index=1,
        action=InvestigationAction(
            action="inspect",
            gap_id=discriminator.gap_id,
            capability=candidate.capability,
            target=candidate.target,
            query=candidate.query,
        ),
        gap_dimension=discriminator.dimension,
        discriminator=InvestigationDiscriminatorAudit(
            gap_id=discriminator.gap_id,
            dimension=discriminator.dimension,
            missing_fact=discriminator.missing_fact,
            support_outcomes=discriminator.support_outcomes,
            comparison_hypothesis_ids=discriminator.comparison_hypothesis_ids,
            comparison_alternative_ids=discriminator.comparison_alternative_ids,
            no_data_outcomes=discriminator.no_data_outcomes,
        ),
        authorization_result="AUTHORIZED",
        backend_execution_status=InvestigationExecutionStatus.SUCCEEDED,
        observation_outcome=outcome,
        resolution_before=Resolution.AMBIGUOUS,
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
    leading_target = _entity("Deployment", "leading-candidate")
    candidates = (
        _candidate(
            "leading-candidate",
            target=leading_target,
            gap_ids=("leading",),
            hypothesis_ids=("h-leading",),
        ),
        _candidate("broad-candidate", gap_ids=tuple(gap.gap_id for gap in broad)),
    )
    diagnosis = diagnosis.model_copy(
        update={"hypothesis": Hypothesis(hypothesis_id="h-leading", causal_actor=leading_target)}
    )
    ranked = rank_observation_candidates(candidates=candidates, diagnosis=diagnosis)
    assert ranked[0].candidate.candidate_id == "leading-candidate"
    assert ranked[0].utility.hypothesis_relevance == 3


def test_known_evidence_risk_ranks_below_fresh_candidate() -> None:
    case = _case()
    gap_a = _gap("known", GapDimension.CHANGE_TIMING)
    gap_b = _gap("fresh", GapDimension.CHANGE_TIMING)
    diagnosis = _diagnosis(case, (gap_a, gap_b))
    known_candidate = _candidate("a-known", gap_ids=("known",))
    known = known_candidate.__class__(
        **{**known_candidate.__dict__, "known_evidence_refs": ("visible:version-1",)}
    )
    fresh = _candidate("z-fresh", gap_ids=("fresh",))

    ranked = rank_observation_candidates(candidates=(known, fresh), diagnosis=diagnosis)

    assert ranked[0].candidate.candidate_id == "z-fresh"
    assert ranked[1].utility.known_evidence_penalty == 1


def test_overlapping_semantic_repeat_ranks_below_fresh_candidate() -> None:
    case = _case()
    repeated = _candidate("a-repeat")
    fresh = _candidate("z-fresh")
    diagnosis = _diagnosis(
        case,
        (
            _gap(
                repeated.gap_ids[0],
                GapDimension.CHANGE_TIMING,
                capability=repeated.capability,
                target=repeated.target,
            ),
            _gap(
                fresh.gap_ids[0],
                GapDimension.CHANGE_TIMING,
                capability=fresh.capability,
                target=fresh.target,
            ),
        ),
    )
    previous = InvestigationLedgerEntry(
        query_id="prior-query",
        gap_id="prior-gap",
        capability=repeated.capability,
        target=repeated.target,
        query=InvestigationQuery(
            start=ONSET - timedelta(minutes=20),
            end=ONSET + timedelta(minutes=20),
            limit=16,
        ),
        returned_evidence_refs=("known:one",),
        already_known_refs=("known:one",),
    )

    ranked = rank_observation_candidates(
        candidates=(repeated, fresh),
        diagnosis=diagnosis,
        previous_investigations=(previous,),
    )

    assert ranked[0].candidate.candidate_id == "z-fresh"
    assert ranked[1].utility.semantic_duplicate_penalty == 1
    assert ranked[1].utility.known_evidence_penalty == 1


def test_equivalent_discrimination_prefers_lower_semantic_duplicate_risk() -> None:
    case = _case()
    repeated = _candidate_with_states("repeat", target=_entity("Deployment", "payment"))
    fresh = _candidate_with_states("fresh", target=_entity("Deployment", "orders"))
    fresh = fresh.__class__(**{**fresh.__dict__, "discriminators": repeated.discriminators})
    diagnosis = _diagnosis(
        case,
        (_gap("gap", GapDimension.CHANGE_TIMING),),
    )

    ranked = rank_observation_candidates(
        candidates=(repeated, fresh),
        diagnosis=diagnosis,
        previous_action_audits=(_prior_audit(repeated),),
    )

    assert ranked[0].candidate.target == fresh.target
    assert ranked[0].utility.discrimination_value == ranked[1].utility.discrimination_value
    assert ranked[1].utility.semantic_duplicate_penalty == 1


def test_discrimination_precedes_known_evidence_novelty() -> None:
    case = _case()
    high = _candidate_with_states(
        "high", support=("h1", "h2"), comparisons=("h3", "h4"), known_fact_keys=("fact",)
    )
    low = _candidate_with_states("low", target=_entity("Deployment", "low"))
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    ranked = rank_observation_candidates(candidates=(low, high), diagnosis=diagnosis)

    assert ranked[0].candidate.candidate_id == "high"
    assert ranked[0].utility.discrimination_value > ranked[1].utility.discrimination_value
    assert ranked[0].utility.known_evidence_penalty > ranked[1].utility.known_evidence_penalty


def test_equal_discrimination_prefers_candidate_without_known_facts() -> None:
    case = _case()
    known = _candidate_with_states("known", known_fact_keys=("entity|fact|value",))
    fresh = _candidate_with_states("fresh", target=_entity("Deployment", "fresh"))
    fresh = fresh.__class__(**{**fresh.__dict__, "discriminators": known.discriminators})
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    ranked = rank_observation_candidates(candidates=(known, fresh), diagnosis=diagnosis)

    assert ranked[0].candidate.candidate_id == "fresh"
    assert ranked[0].utility.discrimination_value == ranked[1].utility.discrimination_value
    assert ranked[1].utility.known_evidence_penalty == 1


def test_equal_discrimination_prefers_unexplored_frontier() -> None:
    case = _case()
    queried = _candidate_with_states("queried", target=_entity("Deployment", "queried"))
    unexplored = _candidate_with_states("unexplored", target=_entity("Deployment", "unexplored"))
    unexplored = unexplored.__class__(
        **{**unexplored.__dict__, "discriminators": queried.discriminators}
    )
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    ranked = rank_observation_candidates(
        candidates=(queried, unexplored),
        diagnosis=diagnosis,
        previous_action_audits=(_prior_audit(queried),),
    )

    assert ranked[0].candidate.candidate_id == "unexplored"
    assert ranked[0].utility.frontier_coverage > ranked[1].utility.frontier_coverage


def test_no_data_is_neutral_but_matching_retry_ranks_lower() -> None:
    case = _case()
    prior_probe = _candidate_with_states("probe", target=_entity("Deployment", "payment"))
    retry = _candidate_with_states("retry", target=prior_probe.target)
    retry = retry.__class__(**{**retry.__dict__, "discriminators": prior_probe.discriminators})
    alternate = _candidate_with_states("alternate", target=_entity("Deployment", "orders"))
    alternate = alternate.__class__(
        **{**alternate.__dict__, "discriminators": prior_probe.discriminators}
    )
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))
    before = diagnosis.model_dump(mode="json")

    ranked = rank_observation_candidates(
        candidates=(retry, alternate),
        diagnosis=diagnosis,
        previous_action_audits=(_prior_audit(prior_probe, outcome=GapOutcomeKind.NO_DATA),),
    )

    assert ranked[0].candidate.candidate_id == "alternate"
    assert ranked[1].utility.semantic_duplicate_penalty == 1
    assert ranked[1].utility.no_data_repeat_penalty == 1
    assert diagnosis.model_dump(mode="json") == before


def test_different_discriminator_is_not_a_semantic_duplicate() -> None:
    case = _case()
    prior_probe = _candidate_with_states("prior", target=_entity("Deployment", "payment"))
    different_question = _candidate_with_states(
        "different", target=prior_probe.target, comparisons=("h3",)
    )
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    ranked = rank_observation_candidates(
        candidates=(different_question,),
        diagnosis=diagnosis,
        previous_action_audits=(_prior_audit(prior_probe),),
    )

    assert ranked[0].utility.semantic_duplicate_penalty == 0


def test_non_discriminative_candidate_cannot_beat_a_discriminative_candidate() -> None:
    case = _case()
    valid = _candidate_with_states("valid")
    blind = valid.__class__(**{**valid.__dict__, "candidate_id": "blind", "discriminators": ()})
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    ranked = rank_observation_candidates(
        candidates=(blind, valid), diagnosis=diagnosis, require_discriminator=True
    )

    assert ranked[0].candidate.candidate_id == "valid"
    assert all(item.candidate.candidate_id != "blind" for item in ranked)


def test_active_choice_falls_back_when_novelty_has_lower_decision_impact() -> None:
    case = _case()
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.EVENT_SEQUENCE),))
    (baseline,) = rank_observation_candidates(
        candidates=(_candidate("baseline"),), diagnosis=diagnosis
    )
    active = replace(
        baseline,
        candidate=replace(baseline.candidate, candidate_id="active"),
        utility=replace(
            baseline.utility,
            discrimination_value=baseline.utility.discrimination_value + 1,
            expected_decision_impact=baseline.utility.expected_decision_impact - 1,
            known_evidence_penalty=0,
        ),
    )

    dominates, reason = active_choice_dominates_baseline(active, baseline)

    assert dominates is False
    assert reason == "active_choice_has_lower_expected_decision_impact"


def test_active_choice_can_replace_baseline_with_stronger_discrimination() -> None:
    case = _case()
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.EVENT_SEQUENCE),))
    (baseline,) = rank_observation_candidates(
        candidates=(_candidate("baseline"),), diagnosis=diagnosis
    )
    active = replace(
        baseline,
        candidate=replace(baseline.candidate, candidate_id="active"),
        utility=replace(
            baseline.utility,
            discrimination_value=baseline.utility.discrimination_value + 1,
        ),
    )

    dominates, reason = active_choice_dominates_baseline(active, baseline)

    assert dominates is True
    assert reason == "stronger_discrimination_without_lower_decision_impact"


def test_active_choice_can_replace_equivalent_baseline_with_lower_repeat_risk() -> None:
    case = _case()
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.EVENT_SEQUENCE),))
    (baseline,) = rank_observation_candidates(
        candidates=(_candidate("baseline"),), diagnosis=diagnosis
    )
    baseline = replace(
        baseline,
        utility=replace(
            baseline.utility,
            semantic_duplicate_penalty=1,
            known_evidence_penalty=1,
        ),
    )
    active = replace(
        baseline,
        candidate=replace(baseline.candidate, candidate_id="active"),
        utility=replace(
            baseline.utility,
            semantic_duplicate_penalty=0,
            known_evidence_penalty=0,
        ),
    )

    dominates, reason = active_choice_dominates_baseline(active, baseline)

    assert dominates is True
    assert reason == "equivalent_epistemic_value_lower_redundancy"


def test_support_and_comparison_ids_without_transition_path_have_zero_elimination() -> None:
    case = _case()
    candidate = _candidate_with_states("history", target=_entity("ConfigMap", "tls"))
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))

    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)

    assert scored.utility.expected_elimination_value == 0
    assert scored.transition_certificates == ()


def test_discovery_only_capability_without_finding_normalizer_gets_no_transition_value() -> None:
    case = _case()
    candidate = _candidate_with_states(
        "incident-events",
        capability="incident_events",
        target=_entity("Namespace", "shop"),
        support=("h1",),
        comparisons=("h2",),
    )
    discriminator = candidate.discriminators[0]
    candidate = replace(
        candidate,
        dimensions=(GapDimension.EVENT_SEQUENCE,),
        discriminators=(replace(discriminator, dimension=GapDimension.EVENT_SEQUENCE),),
    )
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.EVENT_SEQUENCE),))

    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)

    assert scored.utility.expected_elimination_value == 0
    assert scored.transition_certificates == ()


def test_target_bound_normalized_history_path_can_certify_hypothesis_transition() -> None:
    case = _case()
    target = _entity("Deployment", "payment")
    candidate = _candidate_with_states(
        "history",
        target=target,
        support=("h-payment",),
        comparisons=("h-other",),
    )
    diagnosis = _diagnosis(
        case,
        (_gap("gap", GapDimension.CHANGE_TIMING, hypothesis_ids=("h-payment",)),),
        trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=("h-payment",),
            unresolved_hypotheses=("h-payment",),
        ),
    ).model_copy(update={"hypothesis": Hypothesis(hypothesis_id="h-payment", causal_actor=target)})

    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)

    assert scored.utility.expected_elimination_value == 4
    assert len(scored.transition_certificates) == 1
    certificate = scored.transition_certificates[0]
    assert certificate.observation_fact_family == "object_change"
    assert certificate.permitted_normalized_outcomes == (
        FindingKind.CONFIG_CHANGE,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
    )
    assert certificate.affected_state_ids == ("h-payment",)
    assert certificate.normalizer_rule_id == (
        "normalizers._history_findings→signals.change_findings"
    )
    assert certificate.transition_rule_id == (
        "resolution.assess_hypothesis.aligned_initiating_evidence"
    )


def test_existing_normalized_fact_cannot_claim_new_transition_value() -> None:
    case = _case()
    target = _entity("Deployment", "payment")
    candidate = _candidate_with_states(
        "history",
        target=target,
        support=("h-payment",),
        comparisons=("h-other",),
    )
    existing = Finding(
        kind=FindingKind.ROLLOUT_RESTART,
        entity=target,
        at=ONSET - timedelta(minutes=1),
        summary="already represented rollout restart",
    )
    diagnosis = _diagnosis(
        case,
        (_gap("gap", GapDimension.CHANGE_TIMING, hypothesis_ids=("h-payment",)),),
    ).model_copy(
        update={
            "hypothesis": Hypothesis(
                hypothesis_id="h-payment",
                causal_actor=target,
                findings=(existing,),
            ),
            "evidence": (existing,),
        }
    )

    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)

    assert scored.utility.expected_elimination_value == 0
    assert scored.transition_certificates == ()


def test_autoscaling_event_path_certifies_a_linked_unexplored_alternative() -> None:
    case = _case()
    target = _entity("HorizontalPodAutoscaler", "payment")
    candidate = _candidate_with_states(
        "events",
        capability="events",
        target=target,
        support=(),
        comparisons=(),
    )
    original = candidate.discriminators[0]
    candidate = replace(
        candidate,
        dimensions=(GapDimension.AUTOSCALING_TARGET_STATE,),
        discriminators=(
            replace(
                original,
                dimension=GapDimension.AUTOSCALING_TARGET_STATE,
                support_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.SUPPORTS,
                        alternative_ids=("alt-hpa",),
                        condition="normalized HPA failure is observed",
                    ),
                ),
            ),
        ),
    )
    diagnosis = _diagnosis(
        case,
        (_gap("gap", GapDimension.AUTOSCALING_TARGET_STATE),),
        alternatives=(
            StructuralAlternative(
                alternative_id="alt-hpa",
                actor=target,
                role="autoscaling_controller",
                linked_symptoms=("shop/Service/payment",),
            ),
        ),
    )

    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)

    assert scored.utility.expected_elimination_value == 0
    assert scored.utility.expected_decision_impact == 1
    assert scored.transition_certificates[0].observation_fact_family == (
        "autoscaling_failure_event"
    )
    assert scored.transition_certificates[0].affected_state_kind == "STRUCTURAL_ALTERNATIVE"
    assert scored.transition_certificates[0].permitted_normalized_outcomes == (
        FindingKind.AUTOSCALING_FAILURE,
    )


def test_uncertified_structural_elimination_claim_cannot_override_baseline() -> None:
    case = _case()
    diagnosis = _diagnosis(case, (_gap("gap", GapDimension.CHANGE_TIMING),))
    (baseline,) = rank_observation_candidates(
        candidates=(_candidate("baseline"),), diagnosis=diagnosis
    )
    active = replace(
        baseline,
        candidate=replace(baseline.candidate, candidate_id="active"),
        utility=replace(
            baseline.utility,
            expected_elimination_value=3,
            expected_decision_impact=3,
        ),
    )

    dominates, reason = active_choice_dominates_baseline(active, baseline)

    assert dominates is False
    assert reason == "active_elimination_has_no_transition_certificate"


def test_certified_hypothesis_transition_can_replace_equivalent_baseline() -> None:
    case = _case()
    target = _entity("HorizontalPodAutoscaler", "payment")
    active_candidate = _candidate_with_states(
        "active",
        capability="events",
        target=target,
        support=(),
        comparisons=(),
    )
    discriminator = active_candidate.discriminators[0]
    active_candidate = replace(
        active_candidate,
        dimensions=(GapDimension.AUTOSCALING_TARGET_STATE,),
        discriminators=(
            replace(
                discriminator,
                dimension=GapDimension.AUTOSCALING_TARGET_STATE,
                support_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.SUPPORTS,
                        hypothesis_ids=("h-hpa",),
                        alternative_ids=("alt-hpa",),
                        condition="normalized HPA failure is observed",
                    ),
                ),
            ),
        ),
    )
    diagnosis = _diagnosis(
        case,
        (_gap("gap", GapDimension.AUTOSCALING_TARGET_STATE),),
        trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=("h-hpa",),
            unresolved_hypotheses=("h-hpa",),
        ),
        alternatives=(
            StructuralAlternative(
                alternative_id="alt-hpa",
                actor=target,
                role="autoscaling_controller",
                linked_symptoms=("shop/Service/payment",),
            ),
        ),
    ).model_copy(update={"hypothesis": Hypothesis(hypothesis_id="h-hpa", causal_actor=target)})
    (active,) = rank_observation_candidates(candidates=(active_candidate,), diagnosis=diagnosis)
    baseline_candidate = replace(active_candidate, candidate_id="baseline")
    baseline_candidate = replace(
        baseline_candidate,
        discriminators=(
            replace(
                baseline_candidate.discriminators[0],
                support_outcomes=(
                    GapOutcome(
                        kind=GapOutcomeKind.SUPPORTS,
                        alternative_ids=("unmapped-alternative",),
                        condition="no matching state transition exists",
                    ),
                ),
            ),
        ),
    )
    (baseline,) = rank_observation_candidates(candidates=(baseline_candidate,), diagnosis=diagnosis)
    active = replace(
        active,
        utility=replace(
            active.utility,
            discrimination_value=baseline.utility.discrimination_value,
        ),
    )

    dominates, reason = active_choice_dominates_baseline(active, baseline)

    assert active.transition_certificates
    assert dominates is True
    assert reason == "higher_certified_decision_impact"


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


def test_acquisition_cost_breaks_a_complete_discrimination_and_risk_tie() -> None:
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
    assert ranked[0].candidate.candidate_id == "history-candidate"
    assert ranked[0].utility.discrimination_value == ranked[1].utility.discrimination_value
    assert ranked[0].utility.known_evidence_penalty == ranked[1].utility.known_evidence_penalty
    assert ranked[0].utility.cost_tier < ranked[1].utility.cost_tier


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
    ).model_copy(
        update={
            "hypothesis": Hypothesis(hypothesis_id="h", causal_actor=_entity("Deployment", "gap-a"))
        }
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
        "logs",
        "events",
        "resource_pressure",
    )
    assert all(audit.authorization_result == "AUTHORIZED" for audit in result.action_audits)
    assert all(audit.discriminator is not None for audit in result.action_audits)
    assert all(audit.selection_candidates for audit in result.action_audits)
    for audit in result.action_audits:
        selected_trace = tuple(item for item in audit.selection_candidates if item.selected)
        assert len(selected_trace) == 1
        assert selected_trace[0].candidate_id
        assert selected_trace[0].discrimination_value > 0
        assert selected_trace[0].candidate_ordering_key
    assert result.action_audits[1].observation_outcome is GapOutcomeKind.SUPPORTS
    assert result.action_audits[2].observation_outcome is GapOutcomeKind.NO_DATA
    assert result.diagnosis.resolution == result.initial_resolution


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


@pytest.mark.parametrize(
    ("dimension", "capability", "unknown_slots", "fact_families"),
    (
        (
            GapDimension.EVENT_SEQUENCE,
            "incident_events",
            ("causal_actor", "relevant_event", "temporal_sequence"),
            ("incident_event", "event_sequence"),
        ),
        (
            GapDimension.CHANGE_TIMING,
            "incident_changes",
            ("initiating_object", "change_timing"),
            ("incident_change", "object_change_timing"),
        ),
    ),
)
def test_discovery_gap_candidate_has_truth_blind_bounded_discriminator(
    dimension: GapDimension,
    capability: str,
    unknown_slots: tuple[str, ...],
    fact_families: tuple[str, ...],
) -> None:
    from packages.rca.model import AuthorizedQuery

    case = _case()
    target = EntityRef(kind="Namespace", name="shop", namespace="_cluster")
    gap = InformationGap(
        gap_id=f"discovery-{dimension.value.lower()}",
        origin=InformationGapOrigin.DISCOVERY,
        dimension=dimension,
        missing_fact="an incident-scoped bounded fact is not yet known",
        authorized_queries=(AuthorizedQuery(capability=capability, target=target),),
        candidate_tools=(capability,),
        resolvability=GapResolvability.RESOLVABLE,
    )
    diagnosis = _diagnosis(case, (gap,))

    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )
    eligible = rank_observation_candidates(
        candidates=candidates,
        diagnosis=diagnosis,
        require_discriminator=True,
    )

    assert len(candidates) == 1
    assert candidates[0].query.start is not None
    assert candidates[0].query.end is not None
    assert len(eligible) == 1
    discriminator = candidates[0].discriminators[0]
    assert discriminator.kind is InvestigationDiscriminatorKind.DISCOVERY
    assert discriminator.gap_id == gap.gap_id
    assert discriminator.dimension is dimension
    assert discriminator.unknown_slots == unknown_slots
    assert discriminator.expected_fact_families == fact_families
    assert discriminator.support_outcomes == ()
    assert discriminator.comparison_hypothesis_ids == ()
    assert discriminator.comparison_alternative_ids == ()
    assert "future-actor" not in repr(discriminator)
    assert all(outcome in discriminator.possible_outcomes for outcome in ("NO_MATCH", "NO_DATA"))


def test_discovery_discriminator_does_not_authorize_unrelated_capability() -> None:
    from packages.rca.model import AuthorizedQuery

    case = _case()
    target = _entity("Service", "payments")
    gap = InformationGap(
        gap_id="event-discovery",
        origin=InformationGapOrigin.DISCOVERY,
        dimension=GapDimension.EVENT_SEQUENCE,
        missing_fact="an incident event is unknown",
        authorized_queries=(AuthorizedQuery(capability="logs", target=target),),
        candidate_tools=("logs",),
        resolvability=GapResolvability.RESOLVABLE,
    )
    diagnosis = _diagnosis(case, (gap,))

    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )
    eligible = rank_observation_candidates(
        candidates=candidates,
        diagnosis=diagnosis,
        require_discriminator=True,
    )

    assert len(candidates) == 1
    assert candidates[0].discriminators == ()
    assert eligible == ()


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
