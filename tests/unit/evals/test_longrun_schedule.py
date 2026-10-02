"""The independent check's rhythm (m21 §11): a frozen, seeded schedule and the pre-registered metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.evals.live.longrun import (
    PAYMENT_FAULTS,
    Schedule,
    fault_intervals,
    measure_rows,
    rhythm_schedule,
    schedule_problems,
    truth_at,
)

SEED = 20261003  # the first seed from 20261002 whose schedule fits three hours


def test_the_schedule_is_reproducible_from_its_seed_and_round_trips() -> None:
    schedule = rhythm_schedule(SEED)
    assert rhythm_schedule(SEED).sha256 == schedule.sha256
    assert Schedule.load(schedule.document()).sha256 == schedule.sha256


def test_the_rhythm_keeps_its_promises() -> None:
    schedule = rhythm_schedule(SEED)
    assert schedule_problems(schedule) == []
    blocks = {f.block for f in schedule.faults}
    assert {"same-target", "overlap", "long-overlap"} <= blocks
    payment = [f for f in schedule.faults if f.kind in PAYMENT_FAULTS]
    for a in payment:
        for b in payment:
            assert a is b or b.start_seconds >= a.end_seconds or a.start_seconds >= b.end_seconds


def test_a_block_that_does_not_fit_refuses_the_seed() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        rhythm_schedule(SEED, minutes=60)


T0 = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def test_truth_is_every_fault_in_effect_at_the_onset() -> None:
    journal = [
        {"role": "cause_created", "object": "stresschaos ic-0", "at": at(0)},
        {"role": "cause_created", "object": "networkchaos ic-1", "at": at(1)},
        {"role": "cause_removed", "object": "networkchaos ic-1", "at": at(3)},
        {"role": "cause_created", "object": "deployment payment-service env D=900", "at": at(30)},
        {"role": "cause_removed", "object": "deployment payment-service env D-", "at": at(32)},
        {"role": "cause_removed", "object": "stresschaos ic-0", "at": at(20)},
    ]
    intervals = fault_intervals(journal)
    assert truth_at(T0 + timedelta(minutes=2), intervals) == {
        ("StressChaos", "ic-0"),
        ("NetworkChaos", "ic-1"),
    }
    assert truth_at(T0 + timedelta(minutes=31), intervals) == {("Deployment", "payment-service")}
    assert truth_at(T0 + timedelta(minutes=26), intervals) == frozenset()


def _hypothesis(kind: str, name: str, minutes: float) -> dict[str, Any]:
    return {
        "causal_actor": {"kind": kind, "name": name, "namespace": "sre-demo"},
        "episode_onset": at(10),
        "initiating_findings": [{"kind": "FAULT_INJECTION", "at": at(minutes)}],
        "linked_symptoms": ["sre-demo/Deployment/payment-service"],
        "causal_paths": [],
    }


def _event(name: str, reason: str, minutes: float) -> dict[str, Any]:
    return {
        "reason": reason,
        "involvedObject": {"kind": "NetworkChaos", "name": name},
        "firstTimestamp": at(minutes),
    }


def test_demoting_one_of_two_overlapping_true_causes_is_a_hard_failure() -> None:
    old, new = _hypothesis("NetworkChaos", "old", -40), _hypothesis("NetworkChaos", "new", 9)
    document = {
        "leading_actor_display": "COMPETING",
        "leading_actor_tier": "SUPPORTED",
        "leading_actor_candidates": [old["causal_actor"], new["causal_actor"]],
        "hypothesis": old,
        "alternative_hypotheses": [new],
    }
    events = [
        _event("old", "Applied", -40),
        _event("old", "Recovered", -38),
        _event("new", "Applied", 9),
    ]
    onset = T0 + timedelta(minutes=10)
    both = [
        (T0 - timedelta(minutes=41), T0 + timedelta(minutes=30), ("NetworkChaos", "old")),
        (T0 + timedelta(minutes=9), T0 + timedelta(minutes=12), ("NetworkChaos", "new")),
    ]
    metrics = measure_rows([(document, onset)], events, both, timedelta(minutes=5))
    assert metrics["HARD true_cause_demoted"] == 1 and metrics["HARD overlap_cause_demoted"] == 1
    only_new = both[1:]
    metrics = measure_rows([(document, onset)], events, only_new, timedelta(minutes=5))
    assert metrics["fault: competing_to_single"] == 1
    assert not any(key.startswith("HARD") and value for key, value in metrics.items())
