"""m21 §35: a rollout witness's §28 interval is §13's, the new Pod's journaled presence."""

from __future__ import annotations

from rca_builders import at, ref
from test_declared_symptom_coverage import CALLER, CALLER_POD, OWNED_BY, ready

from packages.rca.model import (
    CausalHop,
    CausalWitness,
    Finding,
    FindingKind,
    Lifecycle,
    ObjectVersion,
)
from packages.rca.resolution import _covered_symptoms, _execution_interval

DEPLOYMENT = ref("shop/Deployment/pay")
NEW_POD = ref("shop/Pod/pay-2-a")
OTHER_CALLER_POD = ref("shop/Pod/order-1-b")
ORDER = ref("shop/Deployment/order")


def rollout() -> CausalWitness:
    origin = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=DEPLOYMENT,
        at=at(10),
        summary="rolled out",
        evidence_ids=("object:deployment",),
    )
    return CausalWitness(
        actor=DEPLOYMENT,
        origin=origin,
        mechanism="ROLLOUT_EXECUTION_ON_INCIDENT_SYMPTOM",
        symptom=CALLER,
        path=(
            CausalHop(source=DEPLOYMENT, relation="rolls_out", target=NEW_POD),
            CausalHop(source=NEW_POD, relation="called_by", target=CALLER),
        ),
        onset=at(11),
        evidence_ids=("object:deployment",),
        claim_level="OBSERVED_MECHANISM_CAUSE",
    )


def version(minutes: float, lifecycle: Lifecycle) -> ObjectVersion:
    return ObjectVersion(
        entity=NEW_POD,
        uid="uid-new",
        observed_at=at(minutes),
        body={},
        evidence_id=f"object:new:{minutes}",
        lifecycle=lifecycle,
    )


CREATED = {NEW_POD: (version(10, Lifecycle.CREATED),)}


def test_the_interval_is_the_new_pods_creation_to_its_deletion_or_the_cutoff() -> None:
    assert _execution_interval(rollout(), CREATED, at(14)) == (at(10), at(14))
    deleted = {NEW_POD: (version(10, Lifecycle.CREATED), version(13, Lifecycle.DELETED))}
    assert _execution_interval(rollout(), deleted, at(14)) == (at(10), at(13))


def test_without_the_journal_or_a_creation_there_is_no_interval() -> None:
    assert _execution_interval(rollout()) is None
    assert _execution_interval(rollout(), {}, at(14)) is None
    observed = {NEW_POD: (version(10, Lifecycle.OBSERVED),)}
    assert _execution_interval(rollout(), observed, at(14)) is None
    assert _execution_interval(rollout(), CREATED, None) is None


def test_a_rollout_witness_at_a_service_covers_its_only_ready_pod_and_workload() -> None:
    covered = _covered_symptoms(
        [rollout()], OWNED_BY, {CALLER: {CALLER_POD}}, [ready(CALLER_POD, 9)], CREATED, at(14)
    )
    assert {CALLER, CALLER_POD, ORDER} <= covered
    # without the journal the witness covers only its own symptom, as before §35
    before = _covered_symptoms(
        [rollout()], OWNED_BY, {CALLER: {CALLER_POD}}, [ready(CALLER_POD, 9)]
    )
    assert before == {CALLER}


def test_a_second_ready_caller_pod_keeps_the_rollout_witness_to_its_service() -> None:
    selects = {CALLER: {CALLER_POD, OTHER_CALLER_POD}}
    statuses = [ready(CALLER_POD, 9), ready(OTHER_CALLER_POD, 9)]
    covered = _covered_symptoms([rollout()], OWNED_BY, selects, statuses, CREATED, at(14))
    assert covered == {CALLER}
