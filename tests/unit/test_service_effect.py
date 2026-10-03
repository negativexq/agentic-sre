"""The service-level effect relation (m21 contract §12): paired calls, bound to the exact target pod."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
