"""External causal-behavior acceptance tests for the m21.v2 decision model."""

from datetime import UTC, datetime, timedelta

from packages.rca.demo import demo_source
from packages.rca.engine import diagnose
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.frontier import apply_frontier_progress
from packages.rca.hypotheses import group_candidates
from packages.rca.model import (
    Candidate,
    CausalHop,
    Diagnosis,
    Edge,
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    FrontierStatus,
    GapDimension,
    Hypothesis,
    Resolution,
    RootSupportStatus,
    StructuralAlternative,
    Symptoms,
)
from packages.rca.ranking import Context, RankingConfig
from packages.rca.resolution import change_onset_path_support, resolve_hypotheses
from packages.rca.topology import Topology

ONSET = datetime(2026, 1, 1, tzinfo=UTC)


def entity(name: str) -> EntityRef:
    return EntityRef(kind="Deployment", namespace="test", name=name)


def finding(
    actor: EntityRef,
    *,
    kind: FindingKind = FindingKind.CONFIG_CHANGE,
    uid: str | None = None,
    onset: datetime = ONSET,
) -> Finding:
    return Finding(
        entity=actor,
        kind=kind,
        at=onset - timedelta(seconds=10),
        entity_instance=EntityInstanceRef(entity=actor, uid=uid) if uid else None,
        incident_onset=onset,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="observed",
        evidence_ids=(f"obs:{actor.name}:{uid}:{onset}",),
    )


def claim(name: str = "cause", *, supported: bool = True, linked: bool = True) -> Hypothesis:
    actor, symptom = entity(name), entity("symptom")
    f = finding(actor, kind=FindingKind.CONFIG_CHANGE if supported else FindingKind.FAILURE_EVENT)
    return Hypothesis(
        hypothesis_id=name,
        claim_version="m21.v2",
        causal_actor=actor,
        episode_onset=ONSET,
        mechanism=f.kind.value,
        findings=(f,),
        initiating_findings=(f,) if supported else (),
        symptom_entities=(symptom,),
        causal_paths=((CausalHop(source=actor, relation="configures", target=symptom),),)
        if linked
        else (),
        linked_symptoms=(symptom.canonical,) if linked else (),
        causal_explanation="PATH" if linked else "UNLINKED",
    )


def test_context_invariance_and_promotion() -> None:
    good, context = claim(), claim("context", linked=False)
    before = resolve_hypotheses((good,))
    after = resolve_hypotheses((context, good))
    assert (before.state, before.diagnosis_status, before.leading_hypothesis_ids) == (
        after.state,
        after.diagnosis_status,
        after.leading_hypothesis_ids,
    )
    assert after.context_hypotheses == ("context",)
    promoted = resolve_hypotheses((good, claim("context", supported=False)))
    assert promoted.state is Resolution.AMBIGUOUS
    assert promoted.unresolved_hypotheses == ("context",)


def test_actor_locality_and_evidence_ablation() -> None:
    h = claim()
    assert change_onset_path_support(h).status is RootSupportStatus.FIRED
    for replacement in ((), (finding(entity("foreign")),)):
        changed = h.model_copy(update={"findings": replacement})
        assert change_onset_path_support(changed).status is not RootSupportStatus.FIRED


def test_member_only_path_cannot_support_or_admit() -> None:
    h = claim().model_copy(
        update={
            "causal_paths": (
                (
                    CausalHop(
                        source=entity("cause"), relation="configures", target=entity("member")
                    ),
                ),
            )
        }
    )
    assert resolve_hypotheses((h,)).context_hypotheses == (h.hypothesis_id,)
    assert change_onset_path_support(h).status is RootSupportStatus.NOT_FIRED


def test_real_rival_and_independent_supported_causes_survive_evidence_volume() -> None:
    h = claim()
    rival = claim("rival", supported=False)
    assert resolve_hypotheses((h, rival)).state is Resolution.AMBIGUOUS
    rival = claim("rival")
    enriched = h.model_copy(
        update={"findings": (*h.findings, finding(h.causal_actor, kind=FindingKind.IMAGE_CHANGE))}
    )
    assert resolve_hypotheses((enriched, rival)).state is Resolution.AMBIGUOUS


def test_failure_origin_is_not_initiating_cause() -> None:
    result = resolve_hypotheses((claim(supported=False),))
    assert result.admitted_hypotheses == ("cause",)
    assert not result.plausible_hypotheses
    assert result.claim_level == "UNESTABLISHED"


def test_unknown_dependency_limits_initiating_certainty_even_after_queries() -> None:
    h = claim()
    frontier = StructuralAlternative(
        alternative_id="dep",
        actor=entity("dependency"),
        role="dependency",
        affected_entities=(h.causal_actor,),
        queryable_dimensions=(GapDimension.DEPENDENCY_HEALTH,),
    )
    for status in FrontierStatus:
        result = resolve_hypotheses(
            (h,), structural_alternatives=(frontier.model_copy(update={"status": status}),)
        )
        assert result.state is Resolution.AMBIGUOUS
        assert result.diagnosis_status == "SUPPORTED_CAUSE"
        assert result.material_frontier_ids == ("dep",)
    irrelevant = frontier.model_copy(update={"affected_entities": (entity("elsewhere"),)})
    assert (
        resolve_hypotheses((h,), structural_alternatives=(irrelevant,)).material_frontier_ids == ()
    )


def test_positive_contradiction_remains_audited() -> None:
    h = claim("late")
    late = h.findings[0].model_copy(
        update={
            "at": ONSET + timedelta(hours=1),
            "temporal_role": EvidenceTemporalRole.CONSEQUENCE,
            "details": {"initiating_at": (ONSET + timedelta(hours=1)).isoformat()},
        }
    )
    h = h.model_copy(
        update={"findings": (late,), "initiating_findings": (), "contradictory_findings": (late,)}
    )
    result = resolve_hypotheses((claim(), h))
    assert result.eliminated_hypotheses == ("late",)
    assert result.eliminations[0].evidence_ids == late.evidence_ids


def grouped(
    scores: tuple[float, float] = (1, 10), names: tuple[str, str] = ("a", "b")
) -> tuple[Hypothesis, ...]:
    a, b = (entity(n) for n in names)
    symptom = entity("symptom")
    topology = Topology(
        (
            Edge(source=a, relation="disrupts", target=b),
            Edge(source=b, relation="disrupts", target=symptom),
        ),
        {},
    )
    symptoms = Symptoms(onset=ONSET, last_seen=ONSET, services=(), namespaces=(), alert_names=())
    context = Context(symptoms=symptoms, symptom_entities={symptom}, topology=topology)
    candidates = (
        Candidate(entity=a, score=scores[0], findings=(finding(a),)),
        Candidate(
            entity=b,
            score=scores[1],
            findings=(finding(b), finding(b, kind=FindingKind.FAILURE_EVENT)),
        ),
    )
    return group_candidates(candidates, topology, context, RankingConfig()).hypotheses


def test_multiple_initiators_score_and_identity_invariance() -> None:
    original = grouped()
    changed = grouped((100, 0))
    renamed = grouped(names=("z", "y"))
    assert len(original) == len(changed) == len(renamed) == 2
    assert {h.hypothesis_id for h in original} == {h.hypothesis_id for h in changed}
    for claims in (original, changed, renamed):
        result = resolve_hypotheses(claims)
        assert len(result.admitted_hypotheses) == len(result.plausible_hypotheses) == 2
        assert result.state is Resolution.AMBIGUOUS


def test_instance_and_episode_are_not_combined() -> None:
    actor = entity("actor")
    symptoms = Symptoms(onset=ONSET, last_seen=ONSET, services=(), namespaces=(), alert_names=())
    topology = Topology((), {})
    context = Context(symptoms=symptoms, symptom_entities={actor}, topology=topology)
    findings = (
        finding(actor, uid="old"),
        finding(actor, uid="new"),
        finding(actor, uid="new", onset=ONSET + timedelta(days=1)),
    )
    result = group_candidates(
        (Candidate(entity=actor, score=1, findings=findings),), topology, context, RankingConfig()
    ).hypotheses
    assert len(result) == 3
    assert len({h.hypothesis_key for h in result}) == 3
    assert all(len(h.findings) == 1 for h in result)
    wrong = result[0].model_copy(update={"findings": result[1].findings})
    assert change_onset_path_support(wrong).status is not RootSupportStatus.FIRED


def test_full_inventory_and_serialized_replay_parity() -> None:
    claims = tuple(claim(f"actor-{i}") for i in range(12))
    trace = resolve_hypotheses(claims)
    assert len(trace.hypothesis_audits) == len(trace.leading_hypothesis_ids) == 12
    restored = tuple(Hypothesis.model_validate(h.model_dump(mode="json")) for h in claims)
    assert resolve_hypotheses(restored) == trace
    diagnosis = diagnose(demo_source()).model_copy(
        update={"resolution_trace": trace, "alternative_hypotheses": claims}
    )
    assert diagnosis_epistemic_digest(diagnosis) == diagnosis_epistemic_digest(
        Diagnosis.model_validate(diagnosis.model_dump(mode="json"))
    )


def test_budget_or_error_does_not_close_frontier() -> None:
    f = StructuralAlternative(
        alternative_id="dep",
        actor=entity("dependency"),
        role="dependency",
        affected_entities=(entity("cause"),),
        queryable_dimensions=(GapDimension.DEPENDENCY_HEALTH,),
    )
    # Neither a failed provider read nor exhaustion supplies covered dimensions.
    result = apply_frontier_progress(
        (f,), hypotheses=(claim(),), queried_dimensions_by_alternative={}
    )
    assert result[0].status is FrontierStatus.UNEXPLORED
    assert resolve_hypotheses((claim(),), structural_alternatives=result).material_frontier_ids == (
        "dep",
    )


def test_legacy_claims_are_not_silently_reinterpreted() -> None:
    legacy = claim().model_copy(update={"claim_version": "legacy"})
    result = resolve_hypotheses((legacy,))
    assert not result.admitted_hypotheses
    assert result.hypothesis_audits[0].admission_reasons == ("LEGACY_CLAIM_REQUIRES_REFORMATION",)
