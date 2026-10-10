"""m21 §27 and §28: a workload is covered by its own Pod's witness; a Service with one ready Pod and that Pod are one."""

from __future__ import annotations

from rca_builders import at, ref

from packages.rca.model import CausalHop, CausalWitness, Finding, FindingKind, PodStatusObservation
from packages.rca.resolution import _covered_symptoms

FAULT = ref("shop/NetworkChaos/loss")
POD = ref("shop/Pod/pay-1-a")
OTHER = ref("shop/Pod/pay-1-b")
SERVICE = ref("shop/Service/pay")
CALLER = ref("shop/Service/order")
CALLER_POD = ref("shop/Pod/order-1-a")
OWNED_BY = {
    POD: ref("shop/ReplicaSet/pay-1"),
    OTHER: ref("shop/ReplicaSet/pay-1"),
    ref("shop/ReplicaSet/pay-1"): ref("shop/Deployment/pay"),
    CALLER_POD: ref("shop/ReplicaSet/order-1"),
    ref("shop/ReplicaSet/order-1"): ref("shop/Deployment/order"),
}


def witness(symptom: object = POD, target: object = POD) -> CausalWitness:
    origin = Finding(
        kind=FindingKind.FAULT_INJECTION,
        entity=FAULT,
        at=at(10),
        summary="applied",
        evidence_ids=("evt:applied",),
        details={
            "execution_targets": [
                {
                    "target": f"shop/{target.name}/app",  # type: ignore[attr-defined]
                    "applied_at": at(10).isoformat(),
                    "recovered_at": at(12).isoformat(),
                }
            ]
        },
    )
    return CausalWitness(
        actor=FAULT,
        origin=origin,
        mechanism="FAULT_EXECUTION_ON_INCIDENT_SYMPTOM",
        symptom=symptom,  # type: ignore[arg-type]
        path=(CausalHop(source=FAULT, relation="fault_targets", target=target),),  # type: ignore[arg-type]
        onset=at(11),
        evidence_ids=("evt:applied",),
        claim_level="OBSERVED_MECHANISM_CAUSE",
    )


def ready(pod: object, minutes: float, value: bool = True) -> PodStatusObservation:
    return PodStatusObservation(
        pod=pod,  # type: ignore[arg-type]
        observed_at=at(minutes),
        ready=value,
        evidence_id=f"life:{pod}:{minutes}",
    )


def test_a_workload_is_covered_by_a_witness_on_a_pod_it_owns() -> None:
    covered = _covered_symptoms([witness()], OWNED_BY)
    assert ref("shop/Deployment/pay") in covered
    assert ref("shop/Deployment/order") not in covered
    # ownership only: without the owner record nothing is inferred from the name
    assert ref("shop/Deployment/pay") not in _covered_symptoms([witness()], {})


def test_a_service_whose_only_ready_pod_is_witnessed_is_covered() -> None:
    covered = _covered_symptoms([witness()], OWNED_BY, {SERVICE: {POD}}, [ready(POD, 9)])
    assert SERVICE in covered


def test_a_second_ready_pod_keeps_the_service_uncovered() -> None:
    selects = {SERVICE: {POD, OTHER}}
    inside = [ready(POD, 9), ready(OTHER, 11)]
    assert SERVICE not in _covered_symptoms([witness()], OWNED_BY, selects, inside)
    before = [ready(POD, 9), ready(OTHER, 5)]
    assert SERVICE not in _covered_symptoms([witness()], OWNED_BY, selects, before)
    stopped = [ready(POD, 9), ready(OTHER, 5), ready(OTHER, 9, value=False)]
    assert SERVICE in _covered_symptoms([witness()], OWNED_BY, selects, stopped)


def test_without_the_ledger_nothing_is_covered_through_the_service() -> None:
    assert SERVICE not in _covered_symptoms([witness()], OWNED_BY, {SERVICE: {POD}}, [])


def test_a_service_witness_covers_its_only_ready_pod_and_that_pods_workload() -> None:
    at_caller = witness(symptom=CALLER, target=POD)
    covered = _covered_symptoms(
        [at_caller], OWNED_BY, {CALLER: {CALLER_POD}}, [ready(CALLER_POD, 9)]
    )
    assert {CALLER, CALLER_POD, ref("shop/Deployment/order")} <= covered


def test_a_witness_without_an_execution_interval_covers_only_itself() -> None:
    bare = witness().model_copy(update={"path": ()})
    covered = _covered_symptoms([bare], OWNED_BY, {SERVICE: {POD}}, [ready(POD, 9)])
    assert SERVICE not in covered
