"""Authority withheld when it is not timing-stable (M21 timing contract §5)."""

from __future__ import annotations

from datetime import timedelta

from claim_builders import incident_claim
from rca_builders import deployment, version
from test_causal_closure import fixture as quota_fixture
from test_decisive_evidence import _assess, _stale, _status
from test_fault_execution import _chaos_case
from test_resolution import _ONSET, _entity, _finding, _hpa
from test_timing_assessment import EVENTS, TWO_ONSETS, source_with

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.model import (
    ClaimTiming,
    EvidenceTemporalRole,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    OnsetUncertainty,
    RelationTiming,
    Resolution,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    TimingAssessment,
    TimingStability,
)
from packages.rca.resolution import resolve_hypotheses
from packages.rca.root_cause_eligibility import RootCauseEligibilities
from packages.rca.source import InMemorySource
from packages.rca.timing_stability import (
    RELATION_D1,
    RELATION_ENDED_EPISODE,
    RELATION_EXECUTION,
    RELATION_TEMPORAL_CONTRADICTION,
    TimingMasks,
    applied_timing_masks,
    derive_timing_masks,
)

STABLE, SENSITIVE = TimingStability.STABLE, TimingStability.SENSITIVE


def relation(name: str, stability: TimingStability) -> RelationTiming:
    return RelationTiming(relation=name, stability=stability)


def timing_for(
    key: str,
    *,
    formation: TimingStability = STABLE,
    relations: tuple[RelationTiming, ...] = (),
) -> TimingAssessment:
    return TimingAssessment(
        uncertainty=OnsetUncertainty(reason="ASSESSABLE"),
        status=STABLE,
        claims=(
            ClaimTiming(
                hypothesis_key=key,
                actor="a",
                formation=formation,
                adjudication=STABLE,
                relations=relations,
            ),
        ),
    )


def claim_h() -> Hypothesis:
    return _hpa("h").model_copy(update={"hypothesis_key": "hkey:h", "hypothesis_id": "h"})


def trace_with(*codes: ResolutionReasonCode, strong: bool = False) -> ResolutionTrace:
    return ResolutionTrace(
        state=Resolution.AMBIGUOUS,
        eliminations=tuple(ResolutionElimination(hypothesis_id="h", code=c) for c in codes),
        mechanism_verified_hypotheses=("h",) if strong else (),
    )


def test_a_sensitive_temporal_elimination_is_withheld_and_a_stable_one_stands() -> None:
    code = ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
    trace = trace_with(code)
    sensitive = timing_for(
        "hkey:h", relations=(relation(RELATION_TEMPORAL_CONTRADICTION, SENSITIVE),)
    )
    stable = timing_for("hkey:h", relations=(relation(RELATION_TEMPORAL_CONTRADICTION, STABLE),))
    masked = derive_timing_masks([claim_h()], trace, sensitive)
    assert masked.temporal == frozenset({"h"}) and masked.any()
    assert [(w.authority, w.relations) for w in masked.withheld] == [
        ("TEMPORAL_ELIMINATION", (RELATION_TEMPORAL_CONTRADICTION,))
    ]
    assert not derive_timing_masks([claim_h()], trace, stable).any()


def test_a_sensitive_ended_episode_elimination_is_withheld() -> None:
    trace = trace_with(ResolutionReasonCode.MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET)
    sensitive = timing_for("hkey:h", relations=(relation(RELATION_ENDED_EPISODE, SENSITIVE),))
    assert derive_timing_masks([claim_h()], trace, sensitive).ended == frozenset({"h"})


def test_only_relations_a_claim_was_actually_eliminated_by_are_masked() -> None:
    sensitive = timing_for(
        "hkey:h",
        relations=(
            relation(RELATION_TEMPORAL_CONTRADICTION, SENSITIVE),
            relation(RELATION_ENDED_EPISODE, SENSITIVE),
        ),
    )
    assert not derive_timing_masks([claim_h()], trace_with(), sensitive).any()


def test_strong_authority_needs_formation_d1_and_execution_to_be_stable() -> None:
    trace = trace_with(strong=True)
    stable_relations = (relation(RELATION_D1, STABLE), relation(RELATION_EXECUTION, STABLE))
    assert not derive_timing_masks(
        [claim_h()], trace, timing_for("hkey:h", relations=stable_relations)
    ).any()
    for name in (RELATION_D1, RELATION_EXECUTION):
        relations = tuple(
            relation(r.relation, SENSITIVE if r.relation == name else STABLE)
            for r in stable_relations
        )
        masks = derive_timing_masks([claim_h()], trace, timing_for("hkey:h", relations=relations))
        assert masks.strong == frozenset({"h"})
        assert masks.withheld[0].relations == (name,)
    unstable_formation = derive_timing_masks(
        [claim_h()], trace, timing_for("hkey:h", formation=SENSITIVE, relations=stable_relations)
    )
    assert unstable_formation.withheld[0].relations == ("formation",)


def test_a_strong_claim_without_a_stability_record_is_not_granted_strong_authority() -> None:
    masks = derive_timing_masks([claim_h()], trace_with(strong=True), timing_for("hkey:other"))
    assert masks.strong == frozenset({"h"})


def test_nothing_is_withheld_when_the_onset_was_not_assessed() -> None:
    unassessed = TimingAssessment(uncertainty=OnsetUncertainty(reason="NO_ALERT_CAPTURE_HISTORY"))
    masks = derive_timing_masks(
        [claim_h()],
        trace_with(ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION, strong=True),
        unassessed,
    )
    assert not masks.any()


def contradicted_pair() -> tuple[Hypothesis, Hypothesis]:
    good = _hpa("good")
    late = _finding(
        _entity("Deployment", "late"),
        FindingKind.IMAGE_CHANGE,
        "late-change",
        role=EvidenceTemporalRole.CONSEQUENCE,
        seconds=7200,
    ).model_copy(
        update={
            "details": {
                "source_class": "object_observation",
                "previous_observed_at": (_ONSET + timedelta(hours=1)).isoformat(),
            }
        }
    )
    bad = incident_claim(
        hypothesis_id="hypothesis:late",
        causal_actor=late.entity,
        members=(late.entity,),
        findings=(late,),
        contradictory_findings=(late,),
        causal_explanation="PATH",
        linked_symptoms=("latency",),
        score=100,
    )
    return good, bad


def test_a_masked_temporal_contradiction_leaves_the_claim_unresolved_not_eliminated() -> None:
    good, bad = contradicted_pair()
    plain = resolve_hypotheses((good, bad))
    assert bad.hypothesis_id in plain.eliminated_hypotheses
    with applied_timing_masks(TimingMasks(temporal=frozenset({bad.hypothesis_id}))):
        masked = resolve_hypotheses((good, bad))
    assert bad.hypothesis_id not in masked.eliminated_hypotheses
    states = {
        label: {a.hypothesis_id: a.epistemic_state for a in trace.hypothesis_audits}[
            bad.hypothesis_id
        ]
        for label, trace in (("plain", plain), ("masked", masked))
    }
    assert states["plain"] is HypothesisEpistemicState.CONTRADICTED
    assert states["masked"] is HypothesisEpistemicState.UNRESOLVED
    # The claim keeps its audit record and its reasons; nothing was deleted.
    reasons = {a.hypothesis_id: a.plausibility_reasons for a in masked.hypothesis_audits}
    assert ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION in reasons[bad.hypothesis_id]


def test_masking_a_different_claim_changes_nothing() -> None:
    good, bad = contradicted_pair()
    plain = resolve_hypotheses((good, bad))
    with applied_timing_masks(TimingMasks(temporal=frozenset({"someone-else"}))):
        assert resolve_hypotheses((good, bad)) == plain


def test_the_mask_does_not_outlive_its_context() -> None:
    good, bad = contradicted_pair()
    with applied_timing_masks(TimingMasks(temporal=frozenset({bad.hypothesis_id}))):
        pass
    assert bad.hypothesis_id in resolve_hypotheses((good, bad)).eliminated_hypotheses


def test_a_masked_ended_episode_does_not_eliminate() -> None:
    stale = _stale()
    ended = _assess(statuses=(_status(25),))
    eligibilities = RootCauseEligibilities((), {stale.hypothesis_id: ended})
    plain = resolve_hypotheses((stale,), root_cause_eligibilities=eligibilities)
    assert stale.hypothesis_id in plain.eliminated_hypotheses
    with applied_timing_masks(TimingMasks(ended=frozenset({stale.hypothesis_id}))):
        masked = resolve_hypotheses((stale,), root_cause_eligibilities=eligibilities)
    assert stale.hypothesis_id not in masked.eliminated_hypotheses


def test_masked_strong_authority_leaves_a_possible_cause_and_never_upgrades() -> None:
    source, target, event = quota_fixture()
    strong = resolve_hypotheses((target, source), events=(event,))
    assert strong.diagnosis_status == "MECHANISM_VERIFIED_CAUSE"
    with applied_timing_masks(TimingMasks(strong=frozenset({source.hypothesis_id}))):
        weaker = resolve_hypotheses((target, source), events=(event,))
    assert not weaker.mechanism_verified_hypotheses
    assert weaker.diagnosis_status == "SUPPORTED_CAUSE"
    assert weaker.state is not Resolution.RESOLVED
    assert source.hypothesis_id in weaker.plausible_hypotheses  # possible-cause support kept


def image_change_source() -> InMemorySource:
    """A deployment image change observed between minutes 30 and 40.

    Relative to an onset at minute 6 it is definitely late (after onset plus grace); relative
    to an onset at minute 60 it is a change before the incident.
    """
    source = source_with(*TWO_ONSETS)
    source.event_items = []
    source.versions += [
        version("shop/Deployment/checkout", 30, deployment("checkout"), 1),
        version("shop/Deployment/checkout", 40, deployment("checkout", image="app:2"), 2),
    ]
    return source


def deployment_claim(case: Case) -> Hypothesis:
    return next(h for h in case.hypotheses if h.causal_actor.kind == "Deployment")


def test_the_engine_withholds_an_elimination_that_holds_only_under_the_onset_of_record() -> None:
    source = image_change_source()
    case = build_case(source)
    off = diagnose_case(build_case(source), config=EngineConfig(timing_stability=False))
    on = diagnose_case(case)
    claim = deployment_claim(case)
    assert off.resolution_trace is not None and on.resolution_trace is not None
    assert claim.hypothesis_id in off.resolution_trace.eliminated_hypotheses
    assert claim.hypothesis_id not in on.resolution_trace.eliminated_hypotheses
    assert on.resolution_trace.timing is not None
    assert [(w.hypothesis_key, w.authority) for w in on.resolution_trace.timing.withheld] == [
        (claim.hypothesis_key, "TEMPORAL_ELIMINATION")
    ]
    # Withholding is not deletion: the claim is still audited, now unresolved.
    audit = {a.hypothesis_id: a for a in on.resolution_trace.hypothesis_audits}[claim.hypothesis_id]
    assert audit.epistemic_state is HypothesisEpistemicState.UNRESOLVED


def test_without_uncertainty_the_same_elimination_stands() -> None:
    source = image_change_source()
    source.alert_episode_items = [TWO_ONSETS[0]]  # one admissible onset: nothing can vary
    case = build_case(source)
    trace = diagnose_case(case).resolution_trace
    assert trace is not None and trace.timing is not None
    assert deployment_claim(case).hypothesis_id in trace.eliminated_hypotheses
    assert trace.timing.withheld == ()


def test_a_lone_onset_withholds_nothing() -> None:
    trace = diagnose_case(build_case(_chaos_case(EVENTS))).resolution_trace
    assert trace is not None and trace.timing is not None
    assert trace.timing.withheld == ()
