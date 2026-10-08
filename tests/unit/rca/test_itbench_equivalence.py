"""C11: evidence-backed causal equivalence beside the exact ITBench score, never instead of it."""

from __future__ import annotations

from test_fault_execution import ev
from test_fault_execution_support import EXP, HIT, OFF, applied, failure, recovered, source, spawned

from packages.evals.itbench.benchmark import agent_output
from packages.evals.itbench.contracts import ITBenchGroundTruth, ITBenchGroundTruthGroup
from packages.evals.itbench.equivalence import controller_relations, grade_tracks
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import ClusterEvent, Diagnosis, EntityInstanceRef

SCHEDULE = "chaos/Schedule/checkout-delay"


def diagnose(events: list[ClusterEvent]) -> Diagnosis:
    return diagnose_case(build_case(source(events)), config=OFF)


def truth(*groups: tuple[str, str, str]) -> ITBenchGroundTruth:
    """(kind, namespace, filter) root groups, as ITBench publishes them."""
    return ITBenchGroundTruth(
        scenario_id="Scenario-1",
        root_cause_groups=tuple(
            ITBenchGroundTruthGroup(
                group_id=f"g{i}", kind=kind, namespace=ns, filters=(pattern,), root_cause=True
            )
            for i, (kind, ns, pattern) in enumerate(groups)
        ),
    )


EXPERIMENT = truth(("NetworkChaos", "chaos", "checkout-delay-.*"))


def tracks(events: list[ClusterEvent], gt: ITBenchGroundTruth = EXPERIMENT) -> dict[str, bool]:
    d = diagnose(events)
    graded = grade_tracks("Scenario-1", d, controller_relations(d, events), gt)
    return {name: bool(t["correct"]) for name, t in graded.items()}


def full() -> list[ClusterEvent]:
    return [spawned(), applied(), recovered(), failure(2)]


def test_the_exact_answer_and_the_native_root_cause_do_not_change() -> None:
    d = diagnose(full())
    assert d.root_cause is not None and d.root_cause.canonical == SCHEDULE
    (factor,) = agent_output(d.model_copy(update={"incident_id": "Scenario-1"})).contributing_factor
    assert factor.entity.canonical == SCHEDULE  # what ITBench receives is still the root cause
    assert tracks(full())["exact"] is False


def test_a_proven_spawn_and_execution_make_the_experiment_an_equivalent_answer() -> None:
    d = diagnose(full())
    relations = controller_relations(d, full())
    (instance,) = relations.instances
    assert (instance.entity, instance.uid, instance.executed) == (EXP, "c1", True)
    assert relations.root_uid == "s1" and instance.spawn_evidence_ids
    assert tracks(full()) == {
        "exact": False,
        "controller_record": True,
        "controller_execution": True,
        "executing_instance": True,
    }


def test_without_a_spawn_record_the_experiment_is_its_own_root_and_nothing_is_linked() -> None:
    events = [spawned(named=False), applied(), recovered(), failure(2)]
    d = diagnose(events)
    assert d.root_cause is not None and d.root_cause.canonical == EXP  # the engine's own answer
    relations = controller_relations(d, events)
    assert relations.instances == () and relations.excluded == ("NOT_A_SCHEDULE",)
    result = tracks(events)
    assert result["controller_record"] == result["controller_execution"] == result["exact"]


def test_name_similarity_alone_never_links_an_experiment() -> None:
    # the label names an experiment of the same stem the schedule never named in a record
    other = ev(
        "chaos/NetworkChaos/checkout-delay-zzzzz",
        "Applied",
        1,
        uid="c9",
        message="Successfully apply chaos for shop/checkout-rs-abcde",
    )
    events = [*full(), other]
    gt = truth(("NetworkChaos", "chaos", "^checkout-delay-zzzzz$"))
    result = tracks(events, gt)
    assert not result["controller_record"] and not result["controller_execution"]


def test_a_schedule_instance_other_than_the_root_causes_links_nothing() -> None:
    d = diagnose(full())
    assert d.hypothesis is not None
    other = d.hypothesis.model_copy(
        update={
            "actor_instance": EntityInstanceRef(entity=d.hypothesis.causal_actor, uid="s-other")
        }
    )
    relations = controller_relations(d.model_copy(update={"hypothesis": other}), full())
    assert relations.instances == () and relations.excluded == ("SCHEDULE_INSTANCE_NOT_OBSERVED",)


def test_a_wrong_namespace_or_kind_does_not_match() -> None:
    assert not any(
        v for k, v in tracks(full(), truth(("NetworkChaos", "other", "checkout-delay-.*"))).items()
    )
    assert not any(
        v for k, v in tracks(full(), truth(("StressChaos", "chaos", "checkout-delay-.*"))).items()
    )


def test_two_spawned_executions_are_one_answer_never_two_true_positives() -> None:
    second = [
        ev(SCHEDULE, "Spawned", 2, uid="s1", message="Create new object: checkout-delay-bbbbb"),
        ev("chaos/NetworkChaos/checkout-delay-bbbbb", "Applied", 2, uid="c2", message=HIT),
    ]
    events = [*full(), *second]
    d = diagnose(events)
    relations = controller_relations(d, events)
    assert {i.uid for i in relations.instances} == {"c1", "c2"}
    graded = grade_tracks("Scenario-1", d, relations, EXPERIMENT)["controller_execution"]
    assert (graded["correct"], graded["precision"], graded["recall"]) == (True, 1.0, 1.0)
    assert graded["members"] == 3


def test_a_name_with_two_incarnations_is_ambiguous_and_not_linked() -> None:
    again = ev(EXP, "Applied", 3, uid="c1-again", message=HIT)
    events = [*full(), again]
    d = diagnose(events)
    relations = controller_relations(d, events)
    assert relations.instances == ()
    assert relations.excluded == (f"AMBIGUOUS_INCARNATIONS:{EXP}",)


def test_a_spawned_experiment_that_never_applied_is_structural_only() -> None:
    d = diagnose(full())
    started_only = [
        spawned(),
        ev(EXP, "Started", 1, uid="c1"),
        failure(2),
    ]  # created, never applied
    relations = controller_relations(d, started_only)
    (instance,) = relations.instances
    assert instance.spawn_evidence_ids and not instance.executed
    graded = grade_tracks("Scenario-1", d, relations, EXPERIMENT)
    assert graded["controller_record"]["correct"] and not graded["controller_execution"]["correct"]


def test_an_application_after_the_reference_time_is_not_execution_evidence() -> None:
    late = [spawned(), applied(minute=30), recovered(31), failure(2)]
    d = diagnose(late)
    for instance in controller_relations(d, late).instances:
        assert not instance.executed


def test_several_root_groups_are_scored_with_one_answer() -> None:
    gt = truth(("NetworkChaos", "chaos", "checkout-delay-.*"), ("ConfigMap", "shop", "^flags$"))
    d = diagnose(full())
    graded = grade_tracks("Scenario-1", d, controller_relations(d, full()), gt)[
        "controller_execution"
    ]
    assert (graded["precision"], graded["recall"]) == (1.0, 0.5)
    assert round(graded["f1"], 4) == 0.6667


def test_grading_is_deterministic() -> None:
    d = diagnose(full())
    relations = controller_relations(d, full())
    assert grade_tracks("Scenario-1", d, relations, EXPERIMENT) == grade_tracks(
        "Scenario-1", d, relations, EXPERIMENT
    )
    assert controller_relations(d, full()) == relations


def test_an_execution_from_an_earlier_fault_episode_is_recorded_but_not_counted() -> None:
    d = diagnose(full())  # reference time: minute 6
    earlier = [spawned(), applied(minute=-60), recovered(-59), failure(2)]
    (instance,) = controller_relations(d, earlier).instances
    assert not instance.executed and instance.earlier_execution_evidence_ids
    graded = grade_tracks("Scenario-1", d, controller_relations(d, earlier), EXPERIMENT)
    assert graded["controller_record"]["correct"] and not graded["controller_execution"]["correct"]


def test_an_execution_recovered_within_w_of_the_onset_still_counts() -> None:
    d = diagnose(full())
    recent = [
        spawned(),
        applied(minute=0),
        recovered(2),
        failure(2),
    ]  # recovered 4 min before minute 6
    (instance,) = controller_relations(d, recent).instances
    assert instance.executed
