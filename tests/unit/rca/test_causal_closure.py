"""Causal closure is authorized by raw observations, not labels or graph shape."""

from datetime import UTC, datetime, timedelta

from packages.rca.causal_closure import answer_frontier
from packages.rca.model import (
    CausalHop,
    ClusterEvent,
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    FrontierStatus,
    Hypothesis,
    Resolution,
    StructuralAlternative,
)
from packages.rca.resolution import resolve_hypotheses

AT = datetime(2026, 1, 1, tzinfo=UTC)
QUOTA = EntityRef(kind="ResourceQuota", namespace="shop", name="capacity")
TARGET = EntityRef(kind="ReplicaSet", namespace="shop", name="worker-1")


def fixture() -> tuple[Hypothesis, Hypothesis, ClusterEvent]:
    event = ClusterEvent(
        entity=TARGET,
        reason="FailedCreate",
        type="Warning",
        message="pods forbidden: exceeded quota: capacity",
        first_at=AT,
        last_at=AT,
        evidence_id="event:rejection",
        involved_uid="rs-uid",
    )
    origin = Finding(
        kind=FindingKind.QUOTA_EXCEEDED,
        entity=QUOTA,
        at=AT,
        temporal_role=EvidenceTemporalRole.INITIATING,
        incident_onset=AT,
        summary="observed rejection",
        evidence_ids=("quota:state", event.evidence_id),
        related=(TARGET,),
        details={"rejected": [TARGET.canonical]},
    )
    source = Hypothesis(
        hypothesis_id="quota",
        claim_version="m21.v2",
        causal_actor=QUOTA,
        episode_onset=AT,
        mechanism="QUOTA_EXCEEDED",
        findings=(origin,),
        initiating_findings=(origin,),
        symptom_entities=(TARGET,),
        linked_symptoms=(TARGET.canonical,),
        causal_explanation="PATH",
        causal_paths=((CausalHop(source=QUOTA, relation="quota_blocks", target=TARGET),),),
    )
    instance = EntityInstanceRef(entity=TARGET, uid="rs-uid")
    failure = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=TARGET,
        entity_instance=instance,
        at=AT,
        incident_onset=AT,
        temporal_role=EvidenceTemporalRole.SUPPORTING,
        summary="FailedCreate",
        evidence_ids=(event.evidence_id,),
    )
    target = Hypothesis(
        hypothesis_id="failed-create",
        claim_version="m21.v2",
        causal_actor=TARGET,
        actor_instance=instance,
        episode_onset=AT,
        mechanism="MANIFESTATION_ONLY",
        findings=(failure,),
        symptom_entities=(TARGET,),
        causal_explanation="DIRECT",
    )
    return source, target, event


def test_observed_rejection_explains_effect_and_authorizes_scoped_resolution() -> None:
    source, target, event = fixture()
    trace = resolve_hypotheses((target, source), events=(event,))
    assert trace.state is Resolution.RESOLVED
    assert trace.diagnosis_status == "MECHANISM_VERIFIED_CAUSE"
    assert trace.claim_level == "OBSERVED_MECHANISM_CAUSE"
    assert trace.explained_hypotheses == (target.hypothesis_id,)
    assert trace.explanations[0].evidence_ids == (event.evidence_id,)
    assert trace.explanations[0].consequence == "EXPLAINS_CLAIM"


def test_identical_graph_without_recorded_execution_is_only_possible() -> None:
    source, target, _ = fixture()
    trace = resolve_hypotheses((source, target))
    assert trace.state is Resolution.AMBIGUOUS
    assert not trace.explanations and not trace.mechanism_verified_hypotheses
    assert target.hypothesis_id in trace.unresolved_hypotheses


def test_independent_initiator_on_target_survives_explanation() -> None:
    source, target, event = fixture()
    origin = target.findings[0].model_copy(
        update={
            "kind": FindingKind.IMAGE_CHANGE,
            "evidence_ids": ("independent:rollout",),
            "temporal_role": EvidenceTemporalRole.INITIATING,
        }
    )
    target = target.model_copy(
        update={"findings": (*target.findings, origin), "initiating_findings": (origin,)}
    )
    trace = resolve_hypotheses((source, target), events=(event,))
    assert trace.state is Resolution.AMBIGUOUS
    assert len(trace.plausible_hypotheses) == 2
    # One observed mechanism cannot upgrade the competing D1-only claim.
    assert trace.mechanism_verified_hypotheses == (source.hypothesis_id,)
    assert trace.claim_level == "POSSIBLE_INITIATING_CAUSE"
    assert not trace.explained_hypotheses
    assert trace.explanations[0].consequence == "EXPLAINS_OBSERVATION"


def test_other_observations_and_unknown_time_changes_are_not_erased() -> None:
    source, target, event = fixture()
    other = target.findings[0].model_copy(
        update={
            "kind": FindingKind.ROLLOUT_RESTART,
            "evidence_ids": ("other",),
            "temporal_role": EvidenceTemporalRole.AMBIGUOUS,
        }
    )
    trace = resolve_hypotheses(
        (source, target.model_copy(update={"findings": (*target.findings, other)})), events=(event,)
    )
    assert trace.state is Resolution.AMBIGUOUS
    assert not trace.explained_hypotheses


def test_ablation_uid_episode_and_unrelated_context() -> None:
    source, target, event = fixture()
    context = target.model_copy(
        update={"hypothesis_id": "context", "symptom_entities": (), "causal_paths": ()}
    )
    assert (
        resolve_hypotheses((source, target, context), events=(event,)).state is Resolution.RESOLVED
    )
    for changed in (
        target.model_copy(update={"episode_onset": AT + timedelta(hours=1)}),
        target.model_copy(update={"actor_instance": EntityInstanceRef(entity=TARGET, uid="other")}),
    ):
        assert not resolve_hypotheses((source, changed), events=(event,)).explanations
    missing = source.model_copy(update={"findings": ()})
    trace = resolve_hypotheses((missing, target), events=(event,))
    assert not trace.explanations and not trace.mechanism_verified_hypotheses


def test_frontier_positive_answer_transfer_and_reopening() -> None:
    source, target, event = fixture()
    frontier = StructuralAlternative(
        alternative_id="question",
        actor=QUOTA,
        role="quota",
        material_for_hypothesis_ids=(target.hypothesis_id,),
    )
    relations = resolve_hypotheses((source, target), events=(event,)).explanations
    for status in FrontierStatus:
        assert (
            answer_frontier((frontier.model_copy(update={"status": status}),), (), set())[0].state
            == "OPEN"
        )
    answer = answer_frontier((frontier,), relations, set())[0]
    assert answer.state == "ANSWERED_ROLE_TRANSFERRED"
    assert answer.transferred_claims == (source.hypothesis_id,)
    assert answer.evidence_ids == (event.evidence_id,)
    assert answer_frontier((frontier,), relations, {source.hypothesis_id})[0].state == "OPEN"


def test_duplicate_evidence_is_one_witness_not_independent_confirmation() -> None:
    source, target, event = fixture()
    trace = resolve_hypotheses((source, target), events=(event, event))
    assert trace.explanations[0].evidence_ids == (event.evidence_id,)
    audit = next(a for a in trace.hypothesis_audits if a.hypothesis_id == source.hypothesis_id)
    assert audit.root_support[-1].decisive_evidence_ids == (event.evidence_id,)


def test_two_distinct_observed_mechanisms_are_multiple_causes_not_one_root() -> None:
    source, target, event = fixture()
    quota2 = QUOTA.model_copy(update={"name": "other-capacity"})
    target2 = TARGET.model_copy(update={"name": "other-worker"})
    event2 = event.model_copy(
        update={
            "entity": target2,
            "evidence_id": "event:other",
            "message": "exceeded quota: other-capacity",
        }
    )
    origin2 = source.findings[0].model_copy(
        update={
            "entity": quota2,
            "evidence_ids": (event2.evidence_id,),
            "related": (target2,),
            "details": {"rejected": [target2.canonical]},
        }
    )
    second = source.model_copy(
        update={
            "hypothesis_id": "second",
            "causal_actor": quota2,
            "findings": (origin2,),
            "initiating_findings": (origin2,),
            "symptom_entities": (target2,),
            "causal_paths": ((CausalHop(source=quota2, relation="quota_blocks", target=target2),),),
        }
    )
    trace = resolve_hypotheses((source, second, target), events=(event, event2))
    assert trace.state is Resolution.AMBIGUOUS
    assert trace.diagnosis_status == "MULTIPLE_OBSERVED_CAUSES"
    assert set(trace.independent_mechanism_causes) == {"quota", "second"}
    duplicate = source.model_copy(update={"hypothesis_id": "duplicate"})
    repeated = resolve_hypotheses((source, duplicate, target), events=(event,))
    assert not repeated.independent_mechanism_causes
    assert repeated.state is Resolution.AMBIGUOUS


def test_cyclic_explanations_do_not_answer_frontier_or_resolve() -> None:
    source, _, event = fixture()
    other = QUOTA.model_copy(update={"name": "other-capacity"})
    forward = event.model_copy(update={"entity": other, "involved_uid": None})
    backward = event.model_copy(
        update={
            "entity": QUOTA,
            "involved_uid": None,
            "evidence_id": "event:backward",
            "message": "exceeded quota: other-capacity",
        }
    )
    claims = []
    for hid, actor, remote, outgoing, incoming in [
        ("left", QUOTA, other, forward, backward),
        ("right", other, QUOTA, backward, forward),
    ]:
        origin = source.findings[0].model_copy(
            update={
                "entity": actor,
                "evidence_ids": (outgoing.evidence_id,),
                "related": (remote,),
                "details": {"rejected": [remote.canonical]},
            }
        )
        failure = origin.model_copy(
            update={
                "kind": FindingKind.FAILURE_EVENT,
                "evidence_ids": (incoming.evidence_id,),
                "temporal_role": EvidenceTemporalRole.SUPPORTING,
            }
        )
        claims.append(
            source.model_copy(
                update={
                    "hypothesis_id": hid,
                    "causal_actor": actor,
                    "findings": (origin, failure),
                    "initiating_findings": (origin,),
                    "symptom_entities": (actor, remote),
                    "causal_paths": (
                        (CausalHop(source=actor, relation="quota_blocks", target=remote),),
                    ),
                }
            )
        )
    trace = resolve_hypotheses(claims, events=(forward, backward))
    assert trace.state is Resolution.AMBIGUOUS
    assert not trace.explained_hypotheses
    assert trace.explanations
    assert not trace.independent_mechanism_causes
    assert all("EXPLANATION_CYCLE" in r.remaining_uncertainty for r in trace.explanations)
    frontier = StructuralAlternative(
        alternative_id="cycle", actor=QUOTA, role="quota", material_for_hypothesis_ids=("right",)
    )
    assert answer_frontier((frontier,), trace.explanations, set())[0].state == "OPEN"


def test_answered_question_is_stable_under_unrelated_context() -> None:
    source, target, event = fixture()
    frontier = StructuralAlternative(
        alternative_id="quota-role",
        actor=QUOTA,
        role="quota",
        affected_entities=(TARGET,),
        observation_targets=(QUOTA, TARGET),
    )
    first = resolve_hypotheses(
        (source, target), events=(event,), structural_alternatives=(frontier,)
    )
    unrelated = target.model_copy(
        update={"hypothesis_id": "unrelated", "symptom_entities": (), "causal_paths": ()}
    )
    second = resolve_hypotheses(
        (unrelated, target, source), events=(event,), structural_alternatives=(frontier,)
    )
    assert first.frontier_answers == second.frontier_answers
    assert first.frontier_answers[0].state == "ANSWERED_ROLE_TRANSFERRED"
    assert first.state == second.state == Resolution.RESOLVED


def test_execution_rule_ablation_preserves_only_weaker_authority(monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    import packages.rca.resolution as resolver
    from packages.rca.causal_closure import quota_execution
    from packages.rca.model import RootSupportStatus

    assert isinstance(monkeypatch, MonkeyPatch)
    source, target, event = fixture()
    record = quota_execution(source, resolver.change_onset_path_support(source), (event,))
    monkeypatch.setattr(
        resolver,
        "quota_execution",
        lambda *args: record.model_copy(
            update={
                "status": RootSupportStatus.NOT_FIRED,
                "witnesses": (),
                "decisive_evidence_ids": (),
            }
        ),
    )
    trace = resolver.resolve_hypotheses((source, target), events=(event,))
    assert trace.state is Resolution.AMBIGUOUS
    assert trace.diagnosis_status == "SUPPORTED_CAUSE"
    assert trace.claim_level == "POSSIBLE_INITIATING_CAUSE"
    assert trace.explained_hypotheses == (target.hypothesis_id,)
    assert not trace.mechanism_verified_hypotheses


def test_matching_words_in_non_rejection_event_grant_no_authority() -> None:
    source, target, event = fixture()
    for changed in (
        event.model_copy(update={"type": "Normal", "reason": "SuccessfulCreate"}),
        event.model_copy(update={"reason": "BackOff"}),
    ):
        trace = resolve_hypotheses((source, target), events=(changed,))
        assert trace.state is Resolution.AMBIGUOUS
        assert not trace.mechanism_verified_hypotheses
        assert not trace.explanations


def test_late_observed_rejection_answers_role_without_initiating_support() -> None:
    source, target, event = fixture()
    late = AT + timedelta(hours=1)
    event = event.model_copy(update={"first_at": late, "last_at": late})
    origin = source.findings[0].model_copy(
        update={
            "at": late,
            "temporal_role": EvidenceTemporalRole.CONSEQUENCE,
        }
    )
    source = source.model_copy(update={"findings": (origin,), "initiating_findings": ()})
    target = target.model_copy(
        update={"findings": (target.findings[0].model_copy(update={"at": late}),)}
    )
    trace = resolve_hypotheses((source, target), events=(event,))
    assert trace.state is not Resolution.RESOLVED
    assert not trace.mechanism_verified_hypotheses
    assert source.hypothesis_id in trace.unresolved_hypotheses
    assert target.hypothesis_id in trace.explained_hypotheses
    assert (
        "OBSERVED_ROLE_DOES_NOT_ESTABLISH_INCIDENT_INITIATION"
        in trace.explanations[0].remaining_uncertainty
    )
    question = StructuralAlternative(
        alternative_id="quota-role",
        actor=QUOTA,
        role="quota",
        material_for_hypothesis_ids=(target.hypothesis_id,),
    )
    answer = answer_frontier((question,), trace.explanations, set())[0]
    assert answer.state == "ANSWERED_ROLE_TRANSFERRED"
    assert answer.transferred_claims == (source.hypothesis_id,)
    assert not resolve_hypotheses((source, target)).explained_hypotheses


def test_partial_role_answer_does_not_close_other_bound_claim_questions() -> None:
    source, target, event = fixture()
    trace = resolve_hypotheses((source, target), events=(event,))
    question = StructuralAlternative(
        alternative_id="shared-quota",
        actor=QUOTA,
        role="quota",
        material_for_hypothesis_ids=(target.hypothesis_id, "other-claim"),
    )
    answer = answer_frontier((question,), trace.explanations, set())[0]
    assert answer.state == "OPEN"
    assert answer.affected_claims == (target.hypothesis_id,)
    assert answer.evidence_ids == (event.evidence_id,)
    assert "UPSTREAM_ROLE_PARTIALLY_DETERMINED" in answer.remaining_uncertainty


def test_initiation_timing_contradiction_does_not_erase_observed_later_role() -> None:
    source, target, event = fixture()
    late = AT + timedelta(hours=1)
    event = event.model_copy(update={"first_at": late, "last_at": late})
    origin = source.findings[0].model_copy(
        update={
            "at": late,
            "temporal_role": EvidenceTemporalRole.CONSEQUENCE,
        }
    )
    source = source.model_copy(
        update={
            "findings": (origin,),
            "initiating_findings": (),
            "contradictory_findings": (origin,),
        }
    )
    target = target.model_copy(
        update={"findings": (target.findings[0].model_copy(update={"at": late}),)}
    )
    frontier = StructuralAlternative(
        alternative_id="later-role",
        actor=QUOTA,
        role="quota",
        affected_entities=(TARGET,),
        observation_targets=(QUOTA, TARGET),
    )
    trace = resolve_hypotheses(
        (source, target), events=(event,), structural_alternatives=(frontier,)
    )
    assert source.hypothesis_id in trace.eliminated_hypotheses
    assert target.hypothesis_id in trace.explained_hypotheses
    assert trace.frontier_answers[0].state == "ANSWERED_ROLE_TRANSFERRED"
    assert not trace.plausible_hypotheses and not trace.mechanism_verified_hypotheses
    assert trace.state is not Resolution.RESOLVED
