"""The rollout execution witness (m21 contract §13): a Deployment change's new pods, through the service-level effect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.causal_closure import ROLLOUT_EXECUTION_RULE, rollout_execution
from packages.rca.model import (
    CausalWitness,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    RootSupportRecord,
    RootSupportStatus,
    TraceSpanObservation,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
NS = "shop"
DEPLOYMENT = EntityRef(kind="Deployment", name="payment-service", namespace=NS)
ORDER = EntityRef(kind="Service", name="order-service", namespace=NS)
CHANGE_AT = T0 + timedelta(minutes=9, seconds=30)
ONSET = T0 + timedelta(minutes=11)
OLD_POD = "payment-service-old-1"
NEW_POD = "payment-service-new-1"


def call(i: int, minute: float, seconds: float, pod: str) -> list[TraceSpanObservation]:
    start = T0 + timedelta(minutes=minute)
    end = start + timedelta(seconds=seconds)
    return [
        TraceSpanObservation(
            trace_id=f"t{i}",
            span_id=f"c{i}",
            service="order-service",
            span_kind="CLIENT",
            start_at=start,
            end_at=end,
            evidence_id=f"c{i}",
        ),
        TraceSpanObservation(
            trace_id=f"t{i}",
            span_id=f"s{i}",
            parent_span_id=f"c{i}",
            service="payment-service",
            span_kind="SERVER",
            start_at=start,
            end_at=end,
            semantic_attributes={"k8s.pod.name": pod},
            evidence_id=f"s{i}",
        ),
    ]


BASELINE = [s for i in range(5) for s in call(i, 1.5 + i * 0.5, 0.005, OLD_POD)]
SLOW = [s for i in range(4) for s in call(10 + i, 10.0 + i * 0.2, 1.5, NEW_POD)]
FAST = [s for i in range(4) for s in call(10 + i, 10.0 + i * 0.2, 0.005, NEW_POD)]


def version(
    kind: str, name: str, uid: str, at: datetime, owner: str, lifecycle: Lifecycle
) -> ObjectVersion:
    return ObjectVersion(
        entity=EntityRef(kind=kind, name=name, namespace=NS),
        uid=uid,
        observed_at=at,
        body={"metadata": {"name": name, "uid": uid, "ownerReferences": [{"uid": owner}]}},
        evidence_id=f"object:{name}:{lifecycle.value}",
        lifecycle=lifecycle,
    )


def history(
    *, owner: str = "dep-1", rs_at: datetime = CHANGE_AT + timedelta(seconds=1)
) -> dict[EntityRef, list[ObjectVersion]]:
    rs = version("ReplicaSet", "payment-service-new", "rs-1", rs_at, owner, Lifecycle.CREATED)
    pod = version("Pod", NEW_POD, "pod-1", rs_at + timedelta(seconds=1), "rs-1", Lifecycle.CREATED)
    return {rs.entity: [rs], pod.entity: [pod]}


def holder() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h1",
        causal_actor=DEPLOYMENT,
        actor_instance=EntityInstanceRef(entity=DEPLOYMENT, uid="dep-1"),
        episode_onset=ONSET,
        symptom_entities=(ORDER, EntityRef(kind="Service", name="payment-service", namespace=NS)),
    )


def possible(status: RootSupportStatus = RootSupportStatus.FIRED) -> RootSupportRecord:
    change = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=DEPLOYMENT,
        at=CHANGE_AT,
        summary="Deployment spec changed",
        evidence_ids=("journal:1",),
    )
    d1 = CausalWitness(
        actor=DEPLOYMENT,
        origin=change,
        mechanism="CHANGE",
        symptom=ORDER,
        onset=ONSET,
        evidence_ids=("journal:1",),
    )
    return RootSupportRecord(
        rule_id="m21.support.change-onset-path",
        rule_version="v2",
        support_kind="POSSIBLE_INITIATING_CAUSE",
        status=status,
        witnesses=(d1,) if status is RootSupportStatus.FIRED else (),
    )


def judge(spans: list[TraceSpanObservation], **kwargs: Any) -> RootSupportRecord:
    return rollout_execution(
        holder(),
        kwargs.pop("support", possible()),
        history(**kwargs),
        spans,
        ONSET + timedelta(minutes=3),
    )


def test_slow_calls_to_the_changes_new_pod_give_a_witness_for_the_calling_service() -> None:
    record = judge(BASELINE + SLOW)
    assert record.rule_id == ROLLOUT_EXECUTION_RULE and record.status is RootSupportStatus.FIRED
    (witness,) = record.witnesses
    assert witness.symptom == ORDER  # never the target's own service
    assert witness.claim_level == "OBSERVED_MECHANISM_CAUSE"
    assert witness.relation_evidence_ids == (
        "object:payment-service-new:CREATED",
        f"object:{NEW_POD}:CREATED",
    )
    assert "c10" in witness.evidence_ids


def test_calls_as_fast_as_before_the_change_are_no_effect() -> None:
    assert judge(BASELINE + FAST).status is RootSupportStatus.NOT_FIRED


def test_a_replicaset_owned_by_another_deployment_is_no_execution_of_this_change() -> None:
    assert judge(BASELINE + SLOW, owner="dep-2").status is RootSupportStatus.NOT_FIRED


def test_a_replicaset_created_long_after_the_change_is_not_its_rollout() -> None:
    late = CHANGE_AT + timedelta(seconds=30)
    assert judge(BASELINE + SLOW, rs_at=late).status is RootSupportStatus.NOT_FIRED


def test_no_witness_without_a_supported_change_or_without_traces() -> None:
    assert (
        judge(BASELINE + SLOW, support=possible(RootSupportStatus.NOT_FIRED)).status
        is RootSupportStatus.NOT_FIRED
    )
    assert judge([]).status is RootSupportStatus.NOT_FIRED
