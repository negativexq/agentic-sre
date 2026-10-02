"""Slice 4 (`scheduled-recurring`): the Schedule is the cause, each spawned experiment an execution."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.evals.live.ground_truth import chain_problems
from packages.evals.live.testbed_lab import scheduled_spec
from packages.evals.live.testbed_runner import Injection, derive_parameters, scheduled_chain


def injection(spawned: tuple[tuple[str, str], ...]) -> Injection:
    return Injection(
        "sched-delay-41",
        "uid-s",
        "payment-service-abc-1",
        "uid-p",
        datetime(2026, 10, 3, tzinfo=UTC),
        kind="Schedule",
        spawned=spawned,
    )


def test_the_schedule_is_the_cause_and_every_spawned_experiment_an_execution() -> None:
    chain = scheduled_chain(injection((("sched-delay-41-a", "u1"), ("sched-delay-41-b", "u2"))))
    (cause,) = chain.of_role("cause")
    assert (cause.actor, cause.instance_uid) == ("sre-demo/Schedule/sched-delay-41", "uid-s")
    executions = chain.of_role("execution")
    assert [(e.actor, e.instance_uid) for e in executions] == [
        ("sre-demo/NetworkChaos/sched-delay-41-a", "u1"),
        ("sre-demo/NetworkChaos/sched-delay-41-b", "u2"),
    ]
    assert chain.of_role("propagation") and chain_problems(chain, "scheduled-recurring") == []


def test_the_scheduled_family_draws_a_scheduled_delay_long_enough_for_several_spawns() -> None:
    spec = scheduled_spec(3, (41, 42, 43))
    params = derive_parameters(spec, 41)
    assert params.fault == "scheduled-delay"
    assert 300 <= params.duration_seconds <= 360
    assert (params.spawn_every_seconds, params.spawn_seconds) == (90, 60)
