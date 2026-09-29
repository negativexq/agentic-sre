"""An execution witness plus an incident effect at the exact target carries strong authority."""

from __future__ import annotations

from typing import Any

from rca_builders import alert, at, deployment, event, pod, replicaset, service, version
from test_fault_execution import ev

from packages.rca.causal_closure import FAULT_EXECUTION_RULE
from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.model import ClusterEvent, Hypothesis, ResolutionTrace, RootSupportRecord
from packages.rca.resolution import resolve_hypotheses
from packages.rca.source import InMemorySource
from packages.rca.timing_stability import (
    RELATION_EXECUTION,
    TimingMasks,
    applied_timing_masks,
    claim_views,
)

OFF = EngineConfig(timing_stability=False)
HIT = "Successfully apply chaos for shop/checkout-rs-abcde"
REC = "Successfully recover chaos for shop/checkout-rs-abcde"
EXP = "chaos/NetworkChaos/checkout-delay-aaaaa"


def spawned(minute: float = 1, *, named: bool = True) -> ClusterEvent:
    message = "Create new object: checkout-delay-aaaaa" if named else ""
    return ev("chaos/Schedule/checkout-delay", "Spawned", minute, uid="s1", message=message)


def applied(minute: float = 1, message: str = HIT) -> ClusterEvent:
    return ev(EXP, "Applied", minute, uid="c1", message=message)


def recovered(minute: float = 4) -> ClusterEvent:
    return ev(EXP, "Recovered", minute, uid="c1", message=REC)


def failure(minute: float, uid: str | None = "p1") -> ClusterEvent:
    return event(
        "shop/Pod/checkout-rs-abcde",
        "BackOff",
        minute,
        type_="Warning",
        message="Back-off restarting failed container",
    ).model_copy(update={"involved_uid": uid})


def source(events: list[ClusterEvent]) -> InMemorySource:
    body: dict[str, Any] = pod("checkout-rs-abcde", "checkout", "checkout-rs")
    return InMemorySource(
        name="scheduled-chaos",
        alert_coverage_start=at(0),
        alert_items=[alert("RequestLatency", "checkout", 6)],
        versions=[
            version("shop/Deployment/checkout", 0, deployment("checkout")),
            version("shop/ReplicaSet/checkout-rs", 0, replicaset("checkout-rs", "checkout")),
            version("shop/Pod/checkout-rs-abcde", 0, body),
            version("shop/Service/checkout", 0, service("checkout")),
        ],
        event_items=events,
    )


def full() -> list[ClusterEvent]:
    return [spawned(), applied(), recovered(), failure(2)]


def run(events: list[ClusterEvent]) -> tuple[Case, ResolutionTrace]:
    case = build_case(source(events))
    trace = diagnose_case(case, config=OFF).resolution_trace
    assert trace is not None
    return case, trace


def claim(case: Case, kind: str) -> Hypothesis:
    return next(h for h in case.hypotheses if h.causal_actor.kind == kind)


def record(trace: ResolutionTrace, hypothesis: Hypothesis) -> RootSupportRecord:
    audit = next(a for a in trace.hypothesis_audits if a.hypothesis_id == hypothesis.hypothesis_id)
    return next(r for r in audit.root_support if r.rule_id == FAULT_EXECUTION_RULE)


def fired(events: list[ClusterEvent], kind: str) -> bool:
    case, trace = run(events)
    return record(trace, claim(case, kind)).status.value == "FIRED"


def test_the_schedule_instance_carries_the_witness_of_its_spawned_experiment() -> None:
    case, trace = run(full())
    schedule = claim(case, "Schedule")
    (witness,) = record(trace, schedule).witnesses
    assert witness.actor_instance is not None and witness.actor_instance.uid == "s1"
    assert witness.symptom.name == "checkout-rs-abcde"
    assert witness.attribution == "EXPERIMENT_EXECUTION_CARRIED_BY_SPAWN_RECORD"
    assert witness.relation_evidence_ids == ("evt:chaos/Schedule/checkout-delay:s1:Spawned:1",)
    assert "NO_EFFECT_BEFORE_APPLY" in witness.coverage
    assert "FAULT_ACTION_NOT_OBSERVED" in witness.missing
    assert trace.mechanism_verified_hypotheses == (schedule.hypothesis_id,)
    assert trace.claim_level == "OBSERVED_MECHANISM_CAUSE"


def test_a_service_level_symptom_stays_uncovered_so_the_diagnosis_is_not_resolved() -> None:
    _, trace = run(full())
    assert trace.state.value != "RESOLVED"
    assert trace.diagnosis_status != "MECHANISM_VERIFIED_CAUSE"


def test_spawned_and_applied_alone_confer_no_authority() -> None:
    case, trace = run([spawned(), applied(), recovered()])
    assert not trace.mechanism_verified_hypotheses
    assert record(trace, claim(case, "Schedule")).reasons == (
        "NO_EXECUTION_WITH_INCIDENT_EFFECT_WITNESS",
    )


def test_each_condition_is_needed() -> None:
    assert fired(full(), "Schedule")
    ablations = {
        "no recovered record": [spawned(), applied(), failure(2)],
        "effect before apply": [*full(), failure(0.5)],
        "effect after the interval": [spawned(), applied(), recovered(), failure(5)],
        "fault applied to another pod": [
            spawned(),
            applied(message=HIT.replace("abcde", "zzzzz")),
            recovered(),
            failure(2),
        ],
    }
    assert {name: fired(events, "Schedule") for name, events in ablations.items()} == {
        name: False for name in ablations
    }


def test_the_spawn_record_must_name_the_experiment_for_the_schedule_to_carry_it() -> None:
    events = [spawned(named=False), applied(), recovered(), failure(2)]
    assert not fired(events, "Schedule")


def test_a_target_without_a_pod_uid_is_not_an_exact_effect() -> None:
    assert not fired([spawned(), applied(), recovered(), failure(2, uid=None)], "Schedule")


def test_a_standalone_experiment_carries_its_own_witness() -> None:
    events = [applied(), recovered(), failure(2)]
    case, trace = run(events)
    experiment = claim(case, "NetworkChaos")
    (witness,) = record(trace, experiment).witnesses
    assert witness.attribution == "EXPERIMENT_EXECUTION"
    assert trace.mechanism_verified_hypotheses == (experiment.hypothesis_id,)


def test_an_experiment_with_a_parent_schedule_leaves_the_authority_to_its_parent() -> None:
    case, trace = run(full())
    assert not record(trace, claim(case, "NetworkChaos")).witnesses


def test_the_timing_contract_reads_this_rule_as_the_execution_relation() -> None:
    case, trace = run(full())
    views = claim_views(case.hypotheses, trace)
    schedule = claim(case, "Schedule")
    assert views[schedule.hypothesis_key].relations[RELATION_EXECUTION] == "FIRED"


def test_a_timing_withheld_witness_leaves_a_possible_cause_and_never_upgrades() -> None:
    events = full()
    case = build_case(source(events))
    schedule = claim(case, "Schedule")
    strong = resolve_hypotheses(case.hypotheses, events=events)
    assert strong.mechanism_verified_hypotheses == (schedule.hypothesis_id,)
    with applied_timing_masks(TimingMasks(strong=frozenset({schedule.hypothesis_id}))):
        weaker = resolve_hypotheses(case.hypotheses, events=events)
    assert not weaker.mechanism_verified_hypotheses
    assert schedule.hypothesis_id in weaker.plausible_hypotheses
    assert weaker.claim_level == "POSSIBLE_INITIATING_CAUSE"
