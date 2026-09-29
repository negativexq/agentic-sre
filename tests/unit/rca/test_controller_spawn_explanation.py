"""A schedule instance explains the experiments its controller named by UID."""

from __future__ import annotations

from rca_builders import at
from test_fault_execution import _chaos_case, ev

from packages.rca.causal_closure import SPAWN_EXPLANATION_RULE, answer_frontier, explanations
from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.model import ClusterEvent, Finding, FindingKind, ResolutionTrace

OFF = EngineConfig(timing_stability=False)
HIT = "Successfully apply chaos for shop/checkout-rs-abcde"


def spawned(name: str, minute: float, uid: str = "s1", *, named: bool = True) -> ClusterEvent:
    message = f"Create new object: {name}" if named else ""
    return ev("chaos/Schedule/checkout-delay", "Spawned", minute, uid=uid, message=message)


def applied(name: str, minute: float, uid: str) -> ClusterEvent:
    return ev(f"chaos/NetworkChaos/{name}", "Applied", minute, uid=uid, message=HIT)


def scenario(*, named: bool = True) -> list[ClusterEvent]:
    return [
        spawned("checkout-delay-aaaaa", 1, named=named),
        applied("checkout-delay-aaaaa", 1, "c1"),
        spawned("checkout-delay-bbbbb", 2, named=named),
        applied("checkout-delay-bbbbb", 2, "c2"),
    ]


def diagnose(events: list[ClusterEvent]) -> tuple[Case, ResolutionTrace]:
    case = build_case(_chaos_case(events))
    trace = diagnose_case(case, config=OFF).resolution_trace
    assert trace is not None
    return case, trace


def spawn_relations(trace: ResolutionTrace) -> list:  # type: ignore[type-arg]
    return [r for r in trace.explanations if r.rule_id == SPAWN_EXPLANATION_RULE]


def test_named_experiments_are_explained_and_the_schedule_is_the_one_supported_cause() -> None:
    case, trace = diagnose(scenario())
    relations = spawn_relations(trace)
    assert len(relations) == 2 and {r.consequence for r in relations} == {"EXPLAINS_CLAIM"}
    schedule = next(h for h in case.hypotheses if h.causal_actor.kind == "Schedule")
    assert {r.explaining_claim for r in relations} == {schedule.hypothesis_id}
    assert trace.diagnosis_status == "SUPPORTED_CAUSE"
    assert trace.plausible_hypotheses == (schedule.hypothesis_id,)
    # Two recurring executions of one fault were two rivals before; without the relation:
    assert diagnose(scenario(named=False))[1].diagnosis_status == "COMPETING_CAUSES"


def test_the_witness_is_the_controller_record_and_the_explained_facts_are_the_execution() -> None:
    _, trace = diagnose(scenario())
    first = next(r for r in spawn_relations(trace) if r.manifestation.name.endswith("aaaaa"))
    assert first.evidence_ids == ("evt:chaos/Schedule/checkout-delay:s1:Spawned:1",)
    assert first.explained_evidence_ids == (
        "evt:chaos/NetworkChaos/checkout-delay-aaaaa:c1:Applied:1",
    )
    assert first.coverage == ("EXACT_SPAWN_RECORD_UIDS", "ALL_LOCAL_FACTS_CHECKED")
    assert first.remaining_uncertainty == ("SPAWN_DOES_NOT_ESTABLISH_INCIDENT_INITIATION",)


def test_an_explained_experiment_is_audited_not_deleted_and_says_why() -> None:
    case, trace = diagnose(scenario())
    experiment = next(h for h in case.hypotheses if h.causal_actor.kind == "NetworkChaos")
    assert experiment.hypothesis_id in trace.explained_hypotheses
    assert experiment.hypothesis_id in trace.eliminated_hypotheses
    assert experiment.hypothesis_id in {a.hypothesis_id for a in trace.hypothesis_audits}
    elimination = next(e for e in trace.eliminations if e.hypothesis_id == experiment.hypothesis_id)
    assert elimination.code.value == "POSITIVELY_EXPLAINED_OBSERVATION"
    assert elimination.rule_id == SPAWN_EXPLANATION_RULE
    assert "schedule instance" in elimination.detail and "quota" not in elimination.detail


def test_without_a_controller_record_naming_the_experiment_nothing_is_explained() -> None:
    for events in (
        scenario(named=False),  # records that do not name the child
        [e for e in scenario() if e.reason != "Spawned"],  # no records at all
    ):
        _, trace = diagnose(events)
        assert spawn_relations(trace) == [] and not trace.explained_hypotheses


def test_events_without_uids_never_produce_the_relation() -> None:
    bare = [
        ev(
            "chaos/Schedule/checkout-delay",
            "Spawned",
            1,
            message="Create new object: checkout-delay-aaaaa",
        ),
        ev("chaos/NetworkChaos/checkout-delay-aaaaa", "Applied", 1, message=HIT),
    ]
    _, trace = diagnose(bare)
    assert spawn_relations(trace) == []


def test_each_incarnation_explains_only_its_own_experiments() -> None:
    events = [
        spawned("checkout-delay-aaaaa", 1, uid="s1"),
        applied("checkout-delay-aaaaa", 1, "c1"),
        spawned("checkout-delay-bbbbb", 2, uid="s2"),
        applied("checkout-delay-bbbbb", 2, "c2"),
    ]
    case, trace = diagnose(events)
    by_id = {h.hypothesis_id: h for h in case.hypotheses}
    pairs = {
        (by_id[r.explaining_claim].actor_instance.uid, by_id[r.explained_claim].actor_instance.uid)  # type: ignore[union-attr]
        for r in spawn_relations(trace)
    }
    assert pairs == {("s1", "c1"), ("s2", "c2")}


def test_an_unsupported_schedule_explains_nothing() -> None:
    case, _ = diagnose(scenario())
    assert not [
        r
        for r in explanations(case.hypotheses, set(), (), None)
        if r.rule_id == SPAWN_EXPLANATION_RULE
    ]


def test_an_experiment_with_other_local_facts_stays_in_competition() -> None:
    case, _ = diagnose(scenario())
    experiment = next(h for h in case.hypotheses if h.causal_actor.kind == "NetworkChaos")
    other = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=experiment.causal_actor,
        entity_instance=experiment.actor_instance,
        at=at(3),
        incident_onset=experiment.episode_onset,
        summary="an unrelated local fact",
        evidence_ids=("obs:other",),
    )
    changed = [
        h.model_copy(update={"findings": (*h.findings, other)}) if h is experiment else h
        for h in case.hypotheses
    ]
    supported = {h.hypothesis_id for h in changed if h.causal_actor.kind == "Schedule"}
    relation = next(
        r
        for r in explanations(changed, supported, (), None)
        if r.rule_id == SPAWN_EXPLANATION_RULE and r.explained_claim == experiment.hypothesis_id
    )
    assert relation.consequence == "EXPLAINS_OBSERVATION"
    assert "OTHER_ACTOR_FACTS_REMAIN" in relation.remaining_uncertainty


def test_the_relation_answers_no_frontier_question() -> None:
    case, trace = diagnose(scenario())
    relations = spawn_relations(trace)
    answers = answer_frontier(case.structural_alternatives, relations, set())
    assert not [a for a in answers if a.state == "ANSWERED_ROLE_TRANSFERRED"]
