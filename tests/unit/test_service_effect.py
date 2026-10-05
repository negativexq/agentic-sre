"""The service-level effect relation (m21 contract §12): paired calls, bound to the exact target pod."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.model import TraceSpanObservation
from packages.rca.service_effect import EffectParameters, call_pairs, service_effect

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
PARAMS = EffectParameters(calls=3, factor=3.0, floor=timedelta(seconds=0.2))


def call(
    i: int, minute: float, seconds: float, pod: str = "payment-1", code: str = "201"
) -> list[TraceSpanObservation]:
    start = T0 + timedelta(minutes=minute)
    client = TraceSpanObservation(
        trace_id=f"t{i}",
        span_id=f"c{i}",
        service="order-service",
        span_kind="CLIENT",
        start_at=start,
        end_at=start + timedelta(seconds=seconds),
        semantic_attributes={"http.response.status_code": code},
        evidence_id=f"c{i}",
    )
    server = TraceSpanObservation(
        trace_id=f"t{i}",
        span_id=f"s{i}",
        parent_span_id=f"c{i}",
        service="payment-service",
        span_kind="SERVER",
        start_at=start,
        end_at=start + timedelta(seconds=seconds),
        semantic_attributes={"k8s.pod.name": pod, "http.response.status_code": code},
        evidence_id=f"s{i}",
    )
    return [client, server]


def spans(*calls: list[TraceSpanObservation]) -> list[TraceSpanObservation]:
    return [span for pair in calls for span in pair]


def judge(all_spans: list[TraceSpanObservation], pod: str = "payment-1") -> bool | None:
    pairs = call_pairs(all_spans, "order-service", "payment-service")
    return service_effect(
        pairs,
        target_pod=pod,
        start=T0 + timedelta(minutes=10),
        end=T0 + timedelta(minutes=12),
        baseline=(T0, T0 + timedelta(minutes=5)),
        parameters=PARAMS,
    )


BASELINE = [call(i, i * 0.5, 0.005) for i in range(5)]


def test_calls_to_the_exact_target_far_slower_than_the_baseline_are_an_effect() -> None:
    fault = [call(10 + i, 10.5 + i * 0.2, 2.0) for i in range(4)]
    assert judge(spans(*BASELINE, *fault)) is True


def test_an_unchanged_latency_is_no_effect() -> None:
    fault = [call(10 + i, 10.5 + i * 0.2, 0.006) for i in range(4)]
    assert judge(spans(*BASELINE, *fault)) is False


def test_slow_calls_to_another_replica_are_not_this_targets_effect() -> None:
    other = [call(10 + i, 10.5 + i * 0.2, 2.0, pod="payment-2") for i in range(4)]
    assert (
        judge(spans(*BASELINE, *other)) is None
    )  # no fault call reached the target: unknown, not false


def test_too_few_calls_leave_the_relation_unknown() -> None:
    fault = [call(10 + i, 10.5, 2.0) for i in range(2)]
    assert judge(spans(*BASELINE, *fault)) is None


def test_failures_that_appear_only_with_the_fault_are_an_effect() -> None:
    fault = [call(10 + i, 10.5 + i * 0.2, 0.005, code="503") for i in range(4)]
    assert judge(spans(*BASELINE, *fault)) is True


def test_slow_calls_just_before_the_recorded_start_do_not_count_as_baseline() -> None:
    """An execution acts seconds before its Applied event: those calls are neither fault nor baseline."""
    early = [call(20 + i, 9.9, 2.0) for i in range(4)]  # inside neither window
    fault = [call(10 + i, 10.5 + i * 0.2, 2.0) for i in range(4)]
    assert judge(spans(*BASELINE, *early, *fault)) is True


def test_a_minority_of_slow_calls_in_the_baseline_does_not_hide_the_effect() -> None:
    """An earlier, unrelated slowdown in the baseline window (§12.5) moves the median only if it fills half."""
    contaminated = [*BASELINE, *[call(30 + i, 1 + i * 0.3, 3.3) for i in range(3)]]
    fault = [call(10 + i, 10.5 + i * 0.2, 3.3) for i in range(4)]
    assert judge(spans(*contaminated, *fault)) is True


def test_a_baseline_that_is_already_slow_leaves_the_relation_unknown() -> None:
    slow = [call(i, i * 0.5, 2.0) for i in range(5)]
    fault = [call(10 + i, 10.5 + i * 0.2, 3.3) for i in range(4)]
    assert judge(spans(*slow, *fault)) is None


# ---- the fault-execution rule through the service-level effect (m21 §12.3) ----


def _holder() -> object:
    from packages.rca.model import EntityInstanceRef, EntityRef, Hypothesis

    actor = EntityRef(kind="NetworkChaos", name="delay", namespace="shop")
    return Hypothesis(
        hypothesis_id="h1",
        causal_actor=actor,
        actor_instance=EntityInstanceRef(entity=actor, uid="u1"),
        episode_onset=T0 + timedelta(minutes=11),
        symptom_entities=(
            EntityRef(kind="Service", name="order-service", namespace="shop"),
            EntityRef(kind="Pod", name="order-service-abc-1", namespace="shop"),
        ),
    )


POD = "payment-service-abc-1"  # a Deployment's pod: the target service is read from its name
# m21 §16: the witness's baseline is the five minutes before the fault's first execution (here, minute 10)
JUST_BEFORE = [call(i, 5.5 + i * 0.5, 0.005) for i in range(5)]


def _witnesses(all_spans: list[TraceSpanObservation], pod: str = POD) -> list[Any]:
    from packages.rca.causal_closure import _service_effect_witness
    from packages.rca.model import EntityRef, Finding, FindingKind

    injection = Finding(
        kind=FindingKind.FAULT_INJECTION,
        entity=EntityRef(kind="NetworkChaos", name="delay", namespace="shop"),
        at=T0 + timedelta(minutes=10),
        summary="NetworkChaos applied",
        evidence_ids=("event:1",),
    )
    return _service_effect_witness(
        _holder(),  # type: ignore[arg-type]
        injection,
        EntityRef(kind="Pod", name=pod, namespace="shop"),
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=12),
        all_spans,
    )


def test_slow_calls_from_a_symptom_service_to_the_exact_target_give_a_witness_for_that_service() -> (
    None
):
    fault = [call(10 + i, 10.5 + i * 0.2, 2.0, pod=POD) for i in range(4)]
    (witness,) = _witnesses(spans(*JUST_BEFORE, *fault))
    assert witness.symptom.kind == "Service" and witness.symptom.name == "order-service"
    assert witness.mechanism == "FAULT_EXECUTION_EFFECT_AT_CALLER"
    assert "c10" in witness.evidence_ids  # the calls are the evidence


def test_no_witness_for_calls_to_another_replica_or_without_traces() -> None:
    fault = [call(10 + i, 10.5 + i * 0.2, 2.0, pod="payment-service-abc-2") for i in range(4)]
    assert _witnesses(spans(*JUST_BEFORE, *fault)) == []
    assert _witnesses([]) == []


def test_calls_long_before_the_first_execution_are_not_its_baseline() -> None:
    """§16: a window ten minutes back (another run's traffic, a quiet period) is not the fault's baseline."""
    fault = [call(10 + i, 10.5 + i * 0.2, 2.0, pod=POD) for i in range(4)]
    assert (
        _witnesses(spans(*BASELINE, *fault)) == []
    )  # minutes 0 to 2: before the window, so unknown
