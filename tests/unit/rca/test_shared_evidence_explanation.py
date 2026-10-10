"""m21 §24: a strong claim explains a member of its episode that holds no fact of its own."""

from __future__ import annotations

from rca_builders import at, ref

from packages.rca.causal_closure import SHARED_EVIDENCE_RULE, answer_frontier, explanations
from packages.rca.model import (
    CausalHop,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    StructuralAlternative,
)

ONSET = at(5)
SYMPTOM = ref("shop/Service/order-service")
FAULT = ref("shop/NetworkChaos/dep-loss")
TARGET = ref("shop/Pod/payment-service-abc-1")
OTHER = ref("shop/Pod/order-service-abc-2")


def instance(entity: EntityRef, uid: str) -> EntityInstanceRef:
    return EntityInstanceRef(entity=entity, uid=uid)


def fact(
    entity: EntityRef, uid: str, *evidence: str, kind: FindingKind = FindingKind.FAILURE_EVENT
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        entity_instance=instance(entity, uid),
        at=at(4),
        summary="observed",
        evidence_ids=evidence,
        incident_onset=ONSET,
    )


def claim(
    hid: str,
    actor: EntityRef,
    uid: str,
    findings: list[Finding],
    members: tuple[EntityRef, ...] = (),
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        claim_version="m21.v2",
        causal_actor=actor,
        actor_instance=instance(actor, uid),
        episode_onset=ONSET,
        members=members or (actor,),
        findings=tuple(findings),
        symptom_entities=(SYMPTOM,),
        causal_paths=((CausalHop(source=actor, relation="dependency_of", target=SYMPTOM),),),
    )


LEADER = claim(
    "leader",
    FAULT,
    "f1",
    [
        fact(FAULT, "f1", "evt:applied", kind=FindingKind.FAULT_INJECTION),
        fact(TARGET, "p1", "evt:unhealthy"),
        fact(TARGET, "p1", "trace:errors", kind=FindingKind.DEPENDENCY_ERRORS),
    ],
    members=(FAULT, TARGET, OTHER),
)


def shared(
    *hypotheses: Hypothesis, strong: set[str] | None = None, supported: set[str] | None = None
) -> list:  # type: ignore[type-arg]
    relations = explanations(
        hypotheses,
        supported if supported is not None else {"leader"},
        (),
        None,
        frozenset(),
        strong if strong is not None else {"leader"},
    )
    return [r for r in relations if r.rule_id == SHARED_EVIDENCE_RULE]


def test_a_member_holding_only_the_strong_claims_evidence_is_explained() -> None:
    target = claim(
        "pod",
        TARGET,
        "p1",
        [fact(TARGET, "p1", "evt:unhealthy"), fact(TARGET, "p1", "trace:errors")],
    )
    (relation,) = shared(LEADER, target)
    assert (relation.explaining_claim, relation.explained_claim) == ("leader", "pod")
    assert relation.consequence == "EXPLAINS_CLAIM"
    assert relation.explained_evidence_ids == ("evt:unhealthy", "trace:errors")
    assert relation.coverage == (
        "STRONG_EXPLAINING_CLAIM",
        "EPISODE_MEMBER",
        "ALL_LOCAL_FACTS_SHARED",
    )


def test_a_fact_of_its_own_keeps_the_claim_in_competition() -> None:
    # the eighteenth HOLDOUT's 29: for example an ambiguous CPU reading the strong claim does not hold
    own = claim(
        "pod", OTHER, "o1", [fact(OTHER, "o1", "metric:cpu", kind=FindingKind.RESOURCE_PRESSURE)]
    )
    partly = claim("pod2", TARGET, "p1", [fact(TARGET, "p1", "evt:unhealthy", "evt:restart")])
    assert shared(LEADER, own) == []
    assert shared(LEADER, partly) == []


def test_a_fact_without_evidence_keeps_the_claim_in_competition() -> None:
    bare = claim("pod", TARGET, "p1", [fact(TARGET, "p1", "evt:unhealthy"), fact(TARGET, "p1")])
    assert shared(LEADER, bare) == []


def test_only_a_member_of_the_strong_claims_episode_is_explained() -> None:
    stranger = ref("shop/Pod/checkout-abc-3")
    outside = claim("pod", stranger, "c1", [fact(stranger, "c1", "evt:unhealthy")])
    assert shared(LEADER, outside) == []


def test_without_strong_authority_or_support_nothing_is_explained() -> None:
    target = claim("pod", TARGET, "p1", [fact(TARGET, "p1", "evt:unhealthy")])
    assert shared(LEADER, target, strong=set()) == []
    assert shared(LEADER, target, strong={"leader"}, supported=set()) == []


def test_a_supported_claim_is_never_explained_by_this_rule() -> None:
    target = claim("pod", TARGET, "p1", [fact(TARGET, "p1", "evt:unhealthy")])
    assert shared(LEADER, target, supported={"leader", "pod"}) == []


def test_the_relation_never_answers_a_material_frontier() -> None:
    target = claim("pod", TARGET, "p1", [fact(TARGET, "p1", "evt:unhealthy")])
    relations = shared(LEADER, target)
    question = StructuralAlternative(
        alternative_id="frontier:1",
        role="dependency",
        actor=FAULT,
        material_for_hypothesis_ids=("pod",),
    )
    (answer,) = answer_frontier([question], relations, set())
    assert answer.state == "OPEN"
