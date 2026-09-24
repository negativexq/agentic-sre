"""m16.v1 §11: every elimination names its rule, exact evidence, and preconditions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.causal_roles import HypothesisCausalRole
from packages.rca.model import (
    CausalHop,
    EliminationConsequence,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
)
from packages.rca.resolution import (
    PROPAGATED_EFFECT_RULE,
    TEMPORAL_CONTRADICTION_RULE,
    resolve_hypotheses,
)
from packages.rca.root_cause_eligibility import (
    HypothesisRootCauseEligibility,
    RootCauseEligibilities,
    RootCauseEligibilityBasis,
    RootCauseEligibilityState,
)

_ONSET = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_GRACE = timedelta(minutes=15)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    evidence: str,
    *,
    role: EvidenceTemporalRole = EvidenceTemporalRole.INITIATING,
    seconds: int = -60,
    details: dict[str, object] | None = None,
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=_ONSET + timedelta(seconds=seconds),
        incident_onset=_ONSET,
        onset_delta_seconds=seconds,
        temporal_role=role,
        summary=f"{kind.value} on {entity.name}",
        evidence_ids=(evidence,),
        details={"source_class": "condition", **(details or {})},
    )


def _supported(name: str) -> Hypothesis:
    actor = _entity("HorizontalPodAutoscaler", name)
    target = _entity("Deployment", f"{name}-workload")
    initiating = _finding(actor, FindingKind.AUTOSCALING_FAILURE, f"{name}-hpa")
    return Hypothesis(
        hypothesis_id=f"hypothesis:{name}",
        causal_actor=actor,
        members=(actor, target),
        manifestations=(target,),
        findings=(initiating,),
        initiating_findings=(initiating,),
        causal_paths=((CausalHop(source=actor, relation="scales", target=target),),),
        linked_symptoms=("latency",),
        causal_explanation="PATH",
    )


def _contradicted(late: Finding) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hypothesis:late",
        causal_actor=late.entity,
        members=(late.entity,),
        findings=(late,),
        contradictory_findings=(late,),
        causal_explanation="PATH",
    )


def _only_elimination(trace: ResolutionTrace, hypothesis_id: str) -> ResolutionElimination:
    (item,) = (item for item in trace.eliminations if item.hypothesis_id == hypothesis_id)
    return item


def test_object_change_contradiction_records_the_observation_interval() -> None:
    late = _finding(
        _entity("Deployment", "late"),
        FindingKind.IMAGE_CHANGE,
        "late-change",
        role=EvidenceTemporalRole.CONSEQUENCE,
        seconds=7200,
        details={"previous_observed_at": (_ONSET + timedelta(hours=1)).isoformat()},
    )
    trace = resolve_hypotheses((_supported("good"), _contradicted(late)))

    item = _only_elimination(trace, "hypothesis:late")
    assert item.code is ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
    assert (item.rule_id, item.rule_version) == TEMPORAL_CONTRADICTION_RULE
    assert item.rule == "m16.temporal-contradiction.v1"
    assert item.consequence is EliminationConsequence.CONTRADICTION
    assert item.targets == ("shop/Deployment/late",)
    assert item.mechanism == "INITIATING_TIMING"
    assert item.evidence_ids == ("late-change",)
    (basis,) = item.time_basis
    assert basis.evidence_ids == ("late-change",)
    assert basis.onset == _ONSET and basis.boundary == _ONSET + _GRACE
    # Interval evidence never invents an exact change time.
    assert basis.causal_time is None
    assert basis.interval_start == _ONSET + timedelta(hours=1)
    assert basis.interval_end == _ONSET + timedelta(hours=2)
    assert basis.certainty == "DEFINITELY_LATE"
    assert item.preconditions and all(check.passed for check in item.preconditions)


def test_point_evidence_contradiction_records_its_causal_time() -> None:
    late = _finding(
        _entity("Deployment", "late"),
        FindingKind.FAULT_INJECTION,
        "late-fault",
        role=EvidenceTemporalRole.CONSEQUENCE,
        seconds=3600,
        details={"initiating_at": (_ONSET + timedelta(minutes=50)).isoformat()},
    )
    trace = resolve_hypotheses((_supported("good"), _contradicted(late)))

    (basis,) = _only_elimination(trace, "hypothesis:late").time_basis
    assert basis.causal_time == _ONSET + timedelta(minutes=50)
    assert basis.interval_start is None and basis.interval_end is None


def test_propagated_effect_elimination_keeps_exact_untruncated_provenance() -> None:
    source = _supported("source")
    affected = _supported("affected")
    spans = tuple(f"tempo:trace-{index}:span" for index in range(20))
    eligibility = HypothesisRootCauseEligibility(
        hypothesis_id=affected.hypothesis_id,
        causal_actor=affected.causal_actor,
        causal_role=HypothesisCausalRole.PROPAGATED_EFFECT,
        state=RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT,
        episode_source_capable_initiating_findings=0,
        verified_incoming_edges=3,
        verified_incoming_pairs=1,
        propagation_evidence_ids=spans,
        basis=(
            RootCauseEligibilityBasis.PROPAGATED_EFFECT_CAUSAL_ROLE,
            RootCauseEligibilityBasis.VERIFIED_INCOMING_PROPAGATION,
            RootCauseEligibilityBasis.NO_EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE,
        ),
        rationale="test",
    )
    trace = resolve_hypotheses(
        (source, affected), root_cause_eligibilities=RootCauseEligibilities((eligibility,))
    )

    item = _only_elimination(trace, affected.hypothesis_id)
    assert item.code is ResolutionReasonCode.ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT
    assert (item.rule_id, item.rule_version) == PROPAGATED_EFFECT_RULE
    assert item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    assert item.mechanism == "RUNTIME_PROPAGATION"
    assert item.targets == (affected.causal_actor.canonical,)
    # All twenty trace spans survive; the old record truncated to twelve.
    assert item.evidence_ids == spans
    assert item.observation_ids == spans
    assert "3 verified incoming edge(s)" in item.coverage_basis
    assert [check.name for check in item.preconditions] == [
        "propagated_or_manifestation_role",
        "verified_incoming_propagation",
        "no_episode_source_capable_initiating_evidence",
    ]
    assert all(check.passed for check in item.preconditions)


def test_missing_proof_is_never_an_audited_elimination() -> None:
    unlinked = Hypothesis(
        hypothesis_id="hypothesis:unlinked",
        causal_actor=_entity("Deployment", "unlinked"),
        members=(_entity("Deployment", "unlinked"),),
        causal_explanation="UNLINKED",
    )
    trace = resolve_hypotheses((_supported("good"), unlinked))

    assert trace.eliminations == ()
    assert unlinked.hypothesis_id in trace.unresolved_hypotheses


def test_pre_contract_elimination_records_still_load() -> None:
    legacy = ResolutionElimination.model_validate(
        {
            "hypothesis_id": "hypothesis:old",
            "code": "EXPLICIT_TEMPORAL_CONTRADICTION",
            "evidence_ids": ["old-change"],
            "detail": "contains explicit temporal contradiction",
        }
    )
    assert legacy.rule == "" and legacy.consequence is None and legacy.preconditions == ()
    trace = ResolutionTrace.model_validate(
        {"state": "RESOLVED", "eliminations": [legacy.model_dump(mode="json")]}
    )
    assert trace.eliminations[0].evidence_ids == ("old-change",)
