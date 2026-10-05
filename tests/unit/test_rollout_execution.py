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


def possible(
    status: RootSupportStatus = RootSupportStatus.FIRED, kind: FindingKind = FindingKind.SPEC_CHANGE
) -> RootSupportRecord:
    change = Finding(
        kind=kind,
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


# ---- a failed rollout (m21 §14): the new pod never serves and the old revision is removed ----


def unserved(i: int, minute: float, failed: bool) -> list[TraceSpanObservation]:
    """An order call to payment-service: a failure has no server span, a success has one."""
    from packages.rca.model import TraceSpanStatus

    start = T0 + timedelta(minutes=minute)
    client = TraceSpanObservation(
        trace_id=f"u{i}",
        span_id=f"uc{i}",
        service="order-service",
        span_kind="CLIENT",
        start_at=start,
        end_at=start + timedelta(seconds=0.01),
        status=TraceSpanStatus.ERROR if failed else TraceSpanStatus.UNKNOWN,
        semantic_attributes={"server.address": "payment-service"},
        evidence_id=f"uc{i}",
    )
    if failed:
        return [client]
    server = client.model_copy(
        update={
            "span_id": f"us{i}",
            "parent_span_id": f"uc{i}",
            "service": "payment-service",
            "span_kind": "SERVER",
            "semantic_attributes": {"k8s.pod.name": OLD_POD},
            "evidence_id": f"us{i}",
        }
    )
    return [client, server]


CALM = [s for i in range(5) for s in unserved(i, 1.5 + i * 0.5, failed=False)]
OUTAGE = [s for i in range(4) for s in unserved(10 + i, 10.0 + i * 0.2, failed=True)]


def failed_history(*, old_removed: bool = True) -> dict[EntityRef, list[ObjectVersion]]:
    hist = history()
    old_rs = version("ReplicaSet", "payment-service-old", "rs-0", T0, "dep-1", Lifecycle.CREATED)
    old_pod = [version("Pod", OLD_POD, "pod-0", T0, "rs-0", Lifecycle.CREATED)]
    if old_removed:
        old_pod.append(
            version(
                "Pod", OLD_POD, "pod-0", CHANGE_AT + timedelta(seconds=3), "rs-0", Lifecycle.DELETED
            )
        )
    return {**hist, old_rs.entity: [old_rs], old_pod[0].entity: old_pod}


def backoff(uid: str = "pod-1") -> list[Any]:
    from packages.rca.model import ClusterEvent

    return [
        ClusterEvent(
            entity=EntityRef(kind="Pod", name=NEW_POD, namespace=NS),
            involved_uid=uid,
            reason="BackOff",
            type="Warning",
            message="Back-off restarting failed container",
            first_at=CHANGE_AT + timedelta(seconds=6),
            evidence_id="event:backoff",
        )
    ]


def judge_failed(
    spans: list[TraceSpanObservation], *, events: list[Any] | None = None, old_removed: bool = True
) -> RootSupportRecord:
    from packages.rca.causal_closure import failed_rollout

    return failed_rollout(
        holder(),
        possible(),
        failed_history(old_removed=old_removed),
        backoff() if events is None else events,
        spans,
        ONSET + timedelta(minutes=3),
    )


def test_unserved_calls_after_a_failed_rollout_give_a_witness_for_the_calling_service() -> None:
    from packages.rca.causal_closure import FAILED_ROLLOUT_RULE

    record = judge_failed(CALM + OUTAGE)
    assert record.rule_id == FAILED_ROLLOUT_RULE and record.status is RootSupportStatus.FIRED
    (witness,) = record.witnesses
    assert witness.symptom == ORDER and witness.mechanism == "FAILED_ROLLOUT_EFFECT_AT_CALLER"
    assert "event:backoff" in witness.relation_evidence_ids
    assert any(hop.relation == "rolls_out" and hop.target.name == NEW_POD for hop in witness.path)


def test_no_failed_rollout_without_the_new_pods_own_failure_or_the_old_revisions_removal() -> None:
    assert judge_failed(CALM + OUTAGE, events=[]).status is RootSupportStatus.NOT_FIRED
    assert (
        judge_failed(CALM + OUTAGE, events=backoff("pod-9")).status is RootSupportStatus.NOT_FIRED
    )
    assert judge_failed(CALM + OUTAGE, old_removed=False).status is RootSupportStatus.NOT_FIRED


def test_failures_that_were_already_there_before_the_change_are_no_effect() -> None:
    failing_before = [s for i in range(5) for s in unserved(i, 1.5 + i * 0.5, failed=True)]
    assert judge_failed(failing_before + OUTAGE).status is RootSupportStatus.NOT_FIRED
    assert (
        judge_failed(CALM).status is RootSupportStatus.NOT_FIRED
    )  # nothing failed during the outage


def test_an_image_change_rolls_out_as_a_spec_change_does_and_a_scale_change_does_not() -> None:
    image = judge(BASELINE + SLOW, support=possible(kind=FindingKind.IMAGE_CHANGE))
    scale = judge(BASELINE + SLOW, support=possible(kind=FindingKind.SCALE_CHANGE))
    assert image.status is RootSupportStatus.FIRED
    assert scale.status is RootSupportStatus.NOT_FIRED
