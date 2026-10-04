"""The first HOLDOUT (design §12): variants B, the engine frozen by commit, and blind phase 0."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.evals.live.ground_truth import SuiteManifest, chain_problems
from packages.evals.live.testbed_lab import SPECS, engine_drift
from packages.evals.live.testbed_runner import (
    ERROR_ALERTS,
    LATENCY_ALERTS,
    Injection,
    competing_loss_chain,
    derive_parameters,
    direct_pod_chain,
    loss_chain,
    probe_roles,
    scheduled_stress_chain,
)

AT = datetime(2026, 10, 3, tzinfo=UTC)
VARIANTS_B = {
    "dependency-b": ("dependency-loss-payment", "network-loss"),
    "direct-b": ("direct-stress-payment", "cpu-stress"),
    "scheduled-b": ("scheduled-stress-order", "scheduled-stress"),
    "config-b": ("config-image-payment", "image-break"),
    "negative-b": ("negative-image-decoy", "image-break"),
    "competing-b": ("competing-loss-podkill", "competing-loss"),
}


def test_every_variant_b_is_a_holdout_with_its_own_injection() -> None:
    for key, (scenario_id, fault) in VARIANTS_B.items():
        spec = SPECS[key](3, (1, 2, 3))
        assert (spec.scenario_id, spec.tier) == (scenario_id, "HOLDOUT")
        assert derive_parameters(spec, 1).fault == fault
    assert 80 <= derive_parameters(SPECS["dependency-b"](3, (1, 2, 3)), 1).loss_percent <= 90


def test_a_manifest_without_an_engine_commit_keeps_its_old_digest() -> None:
    spec = SPECS["dependency-b"](3, (1, 2, 3))
    old = SuiteManifest(
        suite_id="s",
        engine_version="2.1.0",
        created_at=AT,
        salt="x",
        scenarios=(spec,),
        acceptance={},
    ).frozen()
    assert old.verified() and "engine_commit" not in old.model_dump_json(exclude={"engine_commit"})
    pinned = old.model_copy(update={"engine_commit": "abc", "sha256": ""}).frozen()
    assert pinned.sha256 != old.sha256 and pinned.verified()


def test_no_engine_commit_means_no_drift_check() -> None:
    assert engine_drift("") == []
    assert engine_drift("HEAD") == [] or all(
        path.startswith(("packages/rca", "apps/control_plane")) for path in engine_drift("HEAD")
    )


def injection(**extra: object) -> Injection:
    return Injection("x", "uid-x", "payment-service-abc-1", "uid-p", AT, **extra)  # type: ignore[arg-type]


def test_a_payment_stress_is_a_direct_fault_on_payment() -> None:
    chain = direct_pod_chain(injection(kind="StressChaos"))
    assert chain.of_role("symptom")[0].actor == "sre-demo/Service/payment-service"
    assert probe_roles("direct-pod-fault", "direct-stress-payment").downstream is None


def test_a_scheduled_stress_spawns_stress_and_has_no_propagation() -> None:
    chain = scheduled_stress_chain(
        injection(kind="Schedule", spawn_kind="StressChaos", spawned=(("x-a", "u1"),))
    )
    assert [link.actor for link in chain.of_role("execution")] == ["sre-demo/StressChaos/x-a"]
    assert not chain.of_role("propagation")
    assert probe_roles("scheduled-recurring", "scheduled-stress-order").downstream is None
    assert chain_problems(chain, "scheduled-recurring") == []


def test_loss_variants_say_loss_and_the_competing_loss_group_takes_errors() -> None:
    assert all("delay" not in link.mechanism for link in loss_chain(injection()).links)
    kill = Injection("pod-kill-1", "uid-k", "order-worker-abc-1", "uid-w", AT, kind="PodChaos")
    chain = competing_loss_chain(injection(companion=kill))
    loss_group = next(g for g in chain.symptom_groups if set(g.alerts) & LATENCY_ALERTS)
    assert ERROR_ALERTS <= set(loss_group.alerts)
    assert chain_problems(chain, "competing-causes") == []
