"""m21 §23: calls that never reached the target, and the fault's action on the witness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.causal_closure import _service_effect_witness, _with_fault_action
from packages.rca.model import (
    CausalWitness,
    EntityInstanceRef,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    ObjectVersion,
    PodStatusObservation,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.service_effect import other_server_ready, unanswered_calls

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
POD = "payment-service-abc-1"
EXPERIMENT = EntityRef(kind="NetworkChaos", name="dep-loss", namespace="shop")


def answered(i: int, minute: float, pod: str = POD) -> list[TraceSpanObservation]:
    start = T0 + timedelta(minutes=minute)
    return [
        TraceSpanObservation(
            trace_id=f"t{i}",
            span_id=f"c{i}",
            service="order-service",
            span_kind="CLIENT",
            start_at=start,
            end_at=start + timedelta(seconds=0.005),
            semantic_attributes={
                "server.address": "payment-service",
                "http.response.status_code": "201",
            },
            evidence_id=f"c{i}",
        ),
        TraceSpanObservation(
            trace_id=f"t{i}",
            span_id=f"s{i}",
            parent_span_id=f"c{i}",
            service="payment-service",
            span_kind="SERVER",
            start_at=start,
            end_at=start + timedelta(seconds=0.005),
            semantic_attributes={"k8s.pod.name": pod, "http.response.status_code": "201"},
            evidence_id=f"s{i}",
        ),
    ]


def unanswered(i: int, minute: float, *, ended: bool = True) -> list[TraceSpanObservation]:
    start = T0 + timedelta(minutes=minute)
    return [
        TraceSpanObservation(
            trace_id=f"u{i}",
            span_id=f"c{i}",
            service="order-service",
            span_kind="CLIENT",
            start_at=start,
            end_at=start + timedelta(seconds=3) if ended else None,
            status=TraceSpanStatus.ERROR,
            semantic_attributes={"server.address": "payment-service"},
            evidence_id=f"u{i}",
        )
    ]


def ready(pod: str, minute: float, is_ready: bool = True) -> PodStatusObservation:
    at = T0 + timedelta(minutes=minute)
    return PodStatusObservation(
        pod=EntityRef(kind="Pod", name=pod, namespace="shop"),
        observed_at=at,
        ready=is_ready,
        ready_since=at,
        evidence_id=f"status:{pod}:{minute}",
    )


def flat(*groups: list[TraceSpanObservation]) -> list[TraceSpanObservation]:
    return [span for group in groups for span in group]


# m21 §16: the baseline is the five minutes before the first execution (minute 10)
BASELINE = [answered(i, 5.5 + i * 0.5) for i in range(5)]
LOSS = [unanswered(10 + i, 10.5 + i * 0.3) for i in range(4)]
ONLY_TARGET = [ready(POD, 4)]


def witnesses(
    spans: list[TraceSpanObservation], statuses: list[PodStatusObservation]
) -> list[CausalWitness]:
    holder = Hypothesis(
        hypothesis_id="h1",
        causal_actor=EXPERIMENT,
        actor_instance=EntityInstanceRef(entity=EXPERIMENT, uid="u1"),
        episode_onset=T0 + timedelta(minutes=11),
        symptom_entities=(EntityRef(kind="Service", name="order-service", namespace="shop"),),
    )
    injection = Finding(
        kind=FindingKind.FAULT_INJECTION,
        entity=EXPERIMENT,
        entity_instance=EntityInstanceRef(entity=EXPERIMENT, uid="u1"),
        at=T0 + timedelta(minutes=10),
        summary="NetworkChaos applied",
        evidence_ids=("event:1",),
    )
    return _service_effect_witness(
        holder,
        injection,
        EntityRef(kind="Pod", name=POD, namespace="shop"),
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=12),
        spans,
        None,
        statuses,
    )


def test_calls_that_never_reached_the_only_server_give_a_witness_for_the_caller() -> None:
    (witness,) = witnesses(flat(*BASELINE, *LOSS), ONLY_TARGET)
    assert witness.symptom.name == "order-service"
    assert "UNANSWERED_CALLS_SINGLE_SERVER" in witness.coverage
    assert {"u10", "u11", "u12", "u13"} <= set(witness.evidence_ids)


def test_another_ready_replica_leaves_the_unanswered_calls_unbound() -> None:
    assert (
        witnesses(flat(*BASELINE, *LOSS), [*ONLY_TARGET, ready("payment-service-abc-2", 9)]) == []
    )


def test_a_replica_ready_before_the_baseline_and_never_seen_to_stop_still_counts() -> None:
    other = [ready("payment-service-abc-2", 1)]
    assert witnesses(flat(*BASELINE, *LOSS), [*ONLY_TARGET, *other]) == []
    stopped = [*other, ready("payment-service-abc-2", 3, is_ready=False)]
    assert len(witnesses(flat(*BASELINE, *LOSS), [*ONLY_TARGET, *stopped])) == 1


def test_a_paired_call_answered_by_another_pod_leaves_the_form_unknown() -> None:
    elsewhere = answered(30, 8.0, pod="payment-service-abc-2")
    assert witnesses(flat(*BASELINE, elsewhere, *LOSS), ONLY_TARGET) == []


def test_unanswered_calls_already_in_the_baseline_are_no_effect_of_the_fault() -> None:
    assert witnesses(flat(*BASELINE, unanswered(40, 7.0), *LOSS), ONLY_TARGET) == []


def test_too_few_unanswered_calls_or_baseline_calls_leave_it_unknown() -> None:
    assert witnesses(flat(*BASELINE, *LOSS[:2]), ONLY_TARGET) == []
    assert witnesses(flat(*BASELINE[:2], *LOSS), ONLY_TARGET) == []


def test_a_ledger_without_the_target_cannot_show_it_was_the_only_server() -> None:
    assert witnesses(flat(*BASELINE, *LOSS), []) == []


def test_an_open_call_has_no_outcome_and_an_answered_error_is_not_unanswered() -> None:
    open_call = unanswered(50, 10.6, ended=False)
    assert unanswered_calls(open_call, "order-service", "payment-service") == []
    answered_error = answered(51, 10.7)
    answered_error[0] = answered_error[0].model_copy(update={"status": TraceSpanStatus.ERROR})
    assert unanswered_calls(answered_error, "order-service", "payment-service") == []


def test_the_ledger_reads_only_pods_of_the_targets_workload() -> None:
    statuses = [*ONLY_TARGET, ready("order-service-abc-9", 9)]
    start, end = T0 + timedelta(minutes=5), T0 + timedelta(minutes=12)
    assert other_server_ready(statuses, POD, "shop", start, end) is False


def _versions(spec: dict[str, Any], uid: str = "u1") -> dict[EntityRef, list[ObjectVersion]]:
    body = {"metadata": {"uid": uid}, "spec": spec}
    return {
        EXPERIMENT: [
            ObjectVersion(entity=EXPERIMENT, observed_at=T0, body=body, evidence_id="object:1")
        ]
    }


def test_the_witness_records_the_experiments_action_and_its_settings() -> None:
    (witness,) = witnesses(flat(*BASELINE, *LOSS), ONLY_TARGET)
    spec = {"action": "loss", "loss": {"loss": "85", "correlation": "0"}, "mode": "all"}
    recorded = _with_fault_action(witness, _versions(spec))
    assert recorded.fault_action == "loss"
    assert recorded.fault_parameters == (("correlation", "0"), ("loss", "85"))
    assert "FAULT_ACTION_OBSERVED" in recorded.coverage
    assert "FAULT_ACTION_NOT_OBSERVED" not in recorded.missing


def test_another_instance_or_no_action_leaves_the_witness_as_it_was() -> None:
    (witness,) = witnesses(flat(*BASELINE, *LOSS), ONLY_TARGET)
    assert _with_fault_action(witness, _versions({"action": "loss"}, uid="u2")) == witness
    assert _with_fault_action(witness, _versions({"stressors": {}})) == witness
