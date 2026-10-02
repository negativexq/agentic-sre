"""The lab's isolation check: which chaos events betray a fault this run did not create."""

from __future__ import annotations

from packages.evals.live.testbed_lab import foreign_fault_event


def test_an_experiment_of_another_run_is_foreign() -> None:
    assert foreign_fault_event("NetworkChaos", "diag-stress", {"dep-delay-11"})
    assert foreign_fault_event("Schedule", "nightly", {"dep-delay-11"})


def test_the_runs_own_experiment_is_not_foreign() -> None:
    assert not foreign_fault_event("NetworkChaos", "dep-delay-11", {"dep-delay-11"})


def test_chaos_meshs_per_pod_record_of_the_runs_experiment_is_not_foreign() -> None:
    """``PodNetworkChaos`` is named after the target pod, not after the experiment it applies."""
    assert not foreign_fault_event(
        "PodNetworkChaos", "payment-service-7fc956765b-t57g8", {"dep-delay-11"}
    )


def test_ordinary_objects_are_never_faults() -> None:
    assert not foreign_fault_event("Pod", "payment-service-7fc956765b-t57g8", set())


def test_an_experiment_spawned_by_the_runs_schedule_is_not_foreign() -> None:
    assert not foreign_fault_event("NetworkChaos", "sched-delay-41-x7k2p", {"sched-delay-41"})
    assert foreign_fault_event("NetworkChaos", "sched-delay-4-x7k2p", {"sched-delay-41"})
    assert foreign_fault_event("NetworkChaos", "other-x7k2p", {"sched-delay-41"})
