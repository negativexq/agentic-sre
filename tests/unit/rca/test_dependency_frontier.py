"""m21 §29 A: a dependency on the material claims' own observed paths is no unknown upstream role."""

from __future__ import annotations

from rca_builders import at, ref

from packages.rca.frontier import DEPENDENCY_ON_CLAIM_PATH_RULE, dependency_on_claim_path
from packages.rca.model import (
    CausalHop,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    StructuralAlternative,
)

ONSET = at(5)
SYMPTOM = ref("shop/Service/order")
PAYMENT = ref("shop/Service/pay")
QUEUE = ref("shop/Service/queue")
FAULT = ref("shop/NetworkChaos/loss")
POD = ref("shop/Pod/pay-1-a")


def claim(hid: str, actor: EntityRef, *hops: EntityRef, version: str = "m21.v2") -> Hypothesis:
    chain = (actor, *hops, SYMPTOM)
    return Hypothesis(
        hypothesis_id=hid,
        claim_version=version,
        causal_actor=actor,
        episode_onset=ONSET,
        findings=(
            Finding(
                kind=FindingKind.FAULT_INJECTION,
                entity=actor,
                at=at(4),
                summary="observed",
                evidence_ids=(f"evt:{hid}",),
                incident_onset=ONSET,
            ),
        ),
        symptom_entities=(SYMPTOM,),
        causal_paths=(
            tuple(
                CausalHop(source=left, relation="dependency_of", target=right)
                for left, right in zip(chain, chain[1:], strict=False)
            ),
        ),
        relation_evidence_ids=(f"path:{hid}",),
    )


def question(actor: EntityRef, *claims: str, role: str = "dependency") -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=f"alternative:{actor.name}",
        actor=actor,
        role=role,
        material_for_hypothesis_ids=claims,
    )


LEADER = claim("leader", FAULT, POD, PAYMENT)
RIVAL = claim("rival", POD, PAYMENT)
ASIDE = claim("aside", ref("shop/Pod/order-1-a"))


def test_a_dependency_on_every_material_claims_path_is_answered() -> None:
    answer = dependency_on_claim_path(question(PAYMENT, "leader", "rival"), [LEADER, RIVAL])
    assert answer is not None
    assert (answer.state, answer.blocked_reason) == ("ANSWERED_ON_CLAIM_PATH", None)
    assert answer.rule_id == DEPENDENCY_ON_CLAIM_PATH_RULE
    assert answer.affected_claims == ("leader", "rival")
    assert answer.evidence_ids == ("path:leader", "path:rival")


def test_a_claim_whose_path_does_not_reach_it_keeps_the_question_open() -> None:
    answer = dependency_on_claim_path(question(PAYMENT, "leader", "aside"), [LEADER, ASIDE])
    assert answer is not None
    assert (answer.state, answer.blocked_reason) == ("OPEN", "NOT_ON_EVERY_CLAIM_PATH")
    assert answer.affected_claims == ("leader",)


def test_a_dependency_off_every_path_is_left_to_the_other_rules() -> None:
    assert dependency_on_claim_path(question(QUEUE, "leader"), [LEADER]) is None


def test_only_the_dependency_role_and_only_validated_paths() -> None:
    assert (
        dependency_on_claim_path(question(PAYMENT, "leader", role="configuration_source"), [LEADER])
        is None
    )
    legacy = claim("legacy", FAULT, POD, PAYMENT, version="m21.v1")
    assert dependency_on_claim_path(question(PAYMENT, "legacy"), [legacy]) is None
    # a claim that is not among the admitted ones is not read
    assert dependency_on_claim_path(question(PAYMENT, "leader"), []) is None


def test_the_investigation_keeps_the_rules_open_condition() -> None:
    from test_fault_execution_support import OFF, full, source

    from packages.rca.engine import build_case, diagnose_case
    from packages.rca.investigation.graph import _investigated_answers
    from packages.rca.model import InvestigationStopReason

    diagnosis = diagnose_case(build_case(source(full())), config=OFF)
    answered = dependency_on_claim_path(question(PAYMENT, "leader"), [LEADER])
    partial = dependency_on_claim_path(
        question(QUEUE, "leader", "aside"), [LEADER, claim("aside", QUEUE)]
    )
    assert answered is not None and partial is not None
    final = {
        a.alternative_id: a
        for a in _investigated_answers(
            diagnosis, (answered, partial), (), InvestigationStopReason.TURN_BUDGET_EXHAUSTED
        )
    }
    assert final["alternative:pay"].investigation_state == "ANSWERED"
    assert final["alternative:queue"].blocked_reason == "NOT_ON_EVERY_CLAIM_PATH"
