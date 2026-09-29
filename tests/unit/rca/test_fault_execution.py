"""Canonical chaos-execution records: instance UIDs, schedule lifecycles, open intervals."""

from __future__ import annotations

from rca_builders import (
    alert,
    at,
    deployment,
    pod,
    ref,
    replicaset,
    service,
    shop_objects,
    version,
)

from packages.rca.engine import build_case
from packages.rca.fault_execution import fault_executions
from packages.rca.model import ClusterEvent, FindingKind
from packages.rca.signals import fault_event_findings
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology, derive_edges

TARGET = "Successfully apply chaos for shop/checkout-5d8f7c9b4-abcde"


def ev(
    value: str,
    reason: str,
    minutes: float,
    *,
    uid: str | None = None,
    message: str = "",
    type_: str = "Normal",
) -> ClusterEvent:
    return ClusterEvent(
        entity=ref(value),
        involved_uid=uid,
        reason=reason,
        type=type_,
        message=message,
        first_at=at(minutes),
        last_at=at(minutes),
        evidence_id=f"evt:{value}:{uid}:{reason}:{minutes}",
    )


def _topology(events: list[ClusterEvent]) -> Topology:
    source = InMemorySource(name="chaos", versions=shop_objects(0), event_items=events)
    latest = {entity: versions[-1] for entity, versions in source.object_history().items()}
    return Topology(derive_edges(latest, events), latest)


def test_applied_target_with_container_and_unobserved_recovery() -> None:
    exp = "chaos/StressChaos/stress-x1y2z"
    events = [
        ev(exp, "Applied", 2, uid="u1", message="Successfully apply chaos for shop/a-1/app"),
        ev(exp, "Recovered", 4, uid="u1", message="Successfully recover chaos for shop/a-1/app"),
        ev(exp, "Applied", 2, uid="u1", message="Successfully apply chaos for shop/b-2/app"),
    ]
    (execution,) = fault_executions(events)
    closed, open_ = execution.targets
    assert (closed.pod, closed.container) == (ref("shop/Pod/a-1"), "app")
    assert closed.recovered_at == at(4)
    assert open_.pod == ref("shop/Pod/b-2")
    # No Recovered event: the interval end is unobserved, never "still active" or closed.
    assert open_.recovered_at is None
    assert open_.applied_at == at(2)


def test_failed_apply_and_failed_recovery_are_counted_separately() -> None:
    exp = "chaos/StressChaos/stress-x1y2z"
    events = [
        ev(exp, "Failed", 1, uid="u1", type_="Warning", message="Failed to apply chaos: rpc error"),
        ev(
            exp,
            "Failed",
            2,
            uid="u1",
            type_="Warning",
            message="Failed to recover chaos: failed to apply for pod shop/a-1: unable to flush",
        ),
    ]
    (execution,) = fault_executions(events)
    assert (execution.failed_apply, execution.failed_recover) == (1, 1)
    assert execution.targets == ()


def test_same_name_schedule_incarnations_are_separate_instances_with_own_children() -> None:
    events = [
        ev(
            "chaos/Schedule/delay", "Spawned", 1, uid="s1", message="Create new object: delay-aaaaa"
        ),
        ev("chaos/NetworkChaos/delay-aaaaa", "Applied", 1, uid="c1", message=TARGET),
        ev(
            "chaos/Schedule/delay",
            "Spawned",
            50,
            uid="s2",
            message="Create new object: delay-bbbbb",
        ),
        ev("chaos/NetworkChaos/delay-bbbbb", "Applied", 50, uid="c2", message=TARGET),
    ]
    executions = {(x.ref.entity.name, x.ref.uid): x for x in fault_executions(events)}
    assert set(executions) == {
        ("delay", "s1"),
        ("delay", "s2"),
        ("delay-aaaaa", "c1"),
        ("delay-bbbbb", "c2"),
    }
    assert executions[("delay-aaaaa", "c1")].schedule.uid == "s1"  # type: ignore[union-attr]
    assert executions[("delay-bbbbb", "c2")].schedule.uid == "s2"  # type: ignore[union-attr]
    assert [c.uid for c in executions[("delay", "s1")].children] == ["c1"]


def test_schedule_window_belongs_to_its_own_incarnation() -> None:
    events = [
        ev(
            "chaos/Schedule/delay", "Spawned", 1, uid="s1", message="Create new object: delay-aaaaa"
        ),
        ev("chaos/NetworkChaos/delay-aaaaa", "Applied", 1, uid="c1", message=TARGET),
        ev(
            "chaos/Schedule/delay",
            "Spawned",
            50,
            uid="s2",
            message="Create new object: delay-bbbbb",
        ),
        ev("chaos/NetworkChaos/delay-bbbbb", "Applied", 50, uid="c2", message=TARGET),
    ]
    findings = fault_event_findings(events, _topology(events))
    by_name = {
        (f.entity.name, f.entity_instance.uid if f.entity_instance else None): f for f in findings
    }
    late = by_name[("delay-bbbbb", "c2")]
    # The later experiment does not inherit the earlier incarnation's start.
    assert late.details["schedule_uid"] == "s2"
    assert late.details["schedule_active_from"] == at(50).isoformat()
    assert by_name[("delay-aaaaa", "c1")].details["schedule_active_from"] == at(1).isoformat()
    schedules = [f for f in findings if f.kind is FindingKind.FAULT_SCHEDULE]
    assert sorted(f.entity_instance.uid for f in schedules if f.entity_instance) == ["s1", "s2"]
    assert {f.at for f in schedules} == {at(1), at(50)}


def test_two_uids_under_one_experiment_name_never_share_a_finding() -> None:
    name = "chaos/NetworkChaos/delay-aaaaa"
    events = [
        ev(name, "Applied", 1, uid="c1", message=TARGET),
        ev(name, "Applied", 30, uid="c2", message=TARGET),
    ]
    findings = fault_event_findings(events, _topology(events))
    assert sorted(f.entity_instance.uid for f in findings if f.entity_instance) == ["c1", "c2"]
    assert all(f.details["reasons"] == {"Applied": 1} for f in findings)


def test_events_without_uid_keep_name_derived_behaviour() -> None:
    events = [
        ev("chaos/Schedule/delay", "Spawned", 1),
        ev("chaos/NetworkChaos/delay-aaaaa", "Applied", 1, message=TARGET),
        ev("chaos/NetworkChaos/delay-aaaaa", "Recovered", 2),
    ]
    findings = {f.entity.name: f for f in fault_event_findings(events, _topology(events))}
    assert findings["delay-aaaaa"].entity_instance is None
    assert findings["delay-aaaaa"].details["schedule"] == "chaos/Schedule/delay"
    assert "schedule_uid" not in findings["delay-aaaaa"].details


def _chaos_case(events: list[ClusterEvent], alert_minute: float = 6) -> InMemorySource:
    return InMemorySource(
        name="scheduled-chaos",
        alert_coverage_start=at(0),
        alert_items=[alert("RequestLatency", "checkout", alert_minute)],
        versions=[
            version("shop/Deployment/checkout", 0, deployment("checkout")),
            version("shop/ReplicaSet/checkout-rs", 0, replicaset("checkout-rs", "checkout")),
            version(
                "shop/Pod/checkout-rs-abcde", 0, pod("checkout-rs-abcde", "checkout", "checkout-rs")
            ),
            version("shop/Service/checkout", 0, service("checkout")),
        ],
        event_items=events,
    )


def test_claims_carry_the_experiment_instance_uid() -> None:
    events = [
        ev("chaos/Schedule/checkout-delay", "Spawned", 1, uid="s1"),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 1, uid="c1", message=TARGET),
    ]
    claims = {h.causal_actor.kind: h for h in build_case(_chaos_case(events)).hypotheses}
    assert claims["NetworkChaos"].actor_instance is not None
    assert claims["NetworkChaos"].actor_instance.uid == "c1"
    assert claims["Schedule"].actor_instance is not None
    assert claims["Schedule"].actor_instance.uid == "s1"


def test_schedule_incarnations_become_separate_claims() -> None:
    events = [
        ev("chaos/Schedule/checkout-delay", "Spawned", 1, uid="s1"),
        ev("chaos/Schedule/checkout-delay", "Spawned", 40, uid="s2"),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 1, uid="c1", message=TARGET),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 40, uid="c2", message=TARGET),
    ]
    hypotheses = build_case(_chaos_case(events)).hypotheses
    uids = sorted(
        h.actor_instance.uid
        for h in hypotheses
        if h.causal_actor.kind == "Schedule" and h.actor_instance is not None
    )
    assert uids == ["s1", "s2"]
    keys = {h.hypothesis_key for h in hypotheses if h.causal_actor.kind == "Schedule"}
    assert len(keys) == 2
