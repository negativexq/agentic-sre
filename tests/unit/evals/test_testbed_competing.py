"""Slice 5 (`competing-causes`, testbed contract §15): two causes, two symptom groups."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.evals.live.ground_truth import chain_problems
from packages.evals.live.testbed_lab import competing_spec
from packages.evals.live.testbed_runner import (
    LAG_ALERTS,
    LATENCY_ALERTS,
    TIMELINE_ALERTS_BY_FAMILY,
    Injection,
    competing_chain,
    derive_parameters,
)

AT = datetime(2026, 10, 3, tzinfo=UTC)


def test_the_chain_has_both_causes_and_a_group_for_each_symptom() -> None:
    kill = Injection("pod-kill-51", "uid-k", "order-worker-abc-1", "uid-w", AT, kind="PodChaos")
    delay = Injection("dep-delay-51", "uid-d", "payment-service-abc-1", "uid-p", AT, companion=kill)
    chain = competing_chain(delay)
    causes = [link.actor for link in chain.of_role("cause")]
    assert causes == ["sre-demo/NetworkChaos/dep-delay-51", "sre-demo/PodChaos/pod-kill-51"]
    latency, lag = chain.symptom_groups
    assert set(latency.alerts) == LATENCY_ALERTS and latency.required == (causes[0],)
    assert set(lag.alerts) == LAG_ALERTS and lag.required == (causes[1],)
    assert set(lag.causes) == set(causes)  # the delay contributes to the lag too
    assert chain_problems(chain, "competing-causes") == []


def test_a_run_whose_second_cause_never_came_is_not_a_competing_chain() -> None:
    lone = Injection("dep-delay-51", "uid-d", "payment-service-abc-1", "uid-p", AT)
    assert chain_problems(competing_chain(lone), "competing-causes")


def test_the_timeline_is_stamped_by_the_latency_alerts_only() -> None:
    assert TIMELINE_ALERTS_BY_FAMILY["competing-causes"] == LATENCY_ALERTS
    params = derive_parameters(competing_spec(3, (51, 52, 53)), 51)
    assert params.fault == "competing" and 0 <= params.second_offset_seconds <= 60
