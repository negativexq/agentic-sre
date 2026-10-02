"""Scoring a testbed run against the chain, using the engine's real diagnosis of a chaos case."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_fault_execution_support import OFF, full, source

from packages.evals.live.ground_truth import (
    Chain,
    Link,
    RunRecord,
    Source,
    Stamp,
    Timeline,
)
from packages.evals.live.testbed_grader import RunScore, aggregate, score_run
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import Diagnosis

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
SCHEDULE = "chaos/Schedule/checkout-delay"
EXPERIMENT = "chaos/NetworkChaos/checkout-delay-aaaaa"
POD = "shop/Pod/checkout-rs-abcde"


def diagnosis() -> Diagnosis:
    return diagnose_case(build_case(source(full())), config=OFF)


def timeline() -> Timeline:
    def stamp(seconds: float, who: Source) -> Stamp:
        return Stamp(at=T0 + timedelta(seconds=seconds), source=who)

    return Timeline(
        cause_created_at=stamp(0, Source.INJECTOR),
        execution_started_at=stamp(2, Source.INJECTOR),
        target_effect_at=stamp(5, Source.ORACLE),
        symptom_started_at=stamp(9, Source.ORACLE),
        alert_fired_at=stamp(40, Source.INJECTOR),
        recovery_at=stamp(120, Source.ORACLE),
    )


def truth_chain(cause: str = SCHEDULE) -> Chain:
    return Chain(
        links=(
            Link(
                role="cause",
                actor=cause,
                instance_uid="s1",
                knowable=True,
                mechanism="NetworkChaos",
            ),
            Link(
                role="execution",
                actor=EXPERIMENT,
                instance_uid="c1",
                knowable=True,
                mechanism="NetworkChaos",
                evidence_class="execution",
            ),
            Link(
                role="target_effect",
                actor=POD,
                knowable=False,
                mechanism="container failure",
                evidence_class="effect",
            ),
        )
    )


def record(chain: Chain, *, offset: float = 0.0, family: str = "direct-pod-fault") -> RunRecord:
    return RunRecord.model_validate(
        {
            "suite_id": "s",
            "scenario_id": "pod",
            "repeat": 0,
            "seed": 1,
            "manifest_sha256": "0" * 64,
            "engine_version": "2.1.0",
            "family": family,
            "clock_offset_seconds": offset,
            "timeline": timeline().model_dump(mode="json"),
            "chain": chain.model_dump(mode="json"),
            "diagnosis_completed_at": (T0 + timedelta(seconds=100)).isoformat(),
            "invalid_reasons": [],
        }
    )


def test_a_diagnosis_that_captures_the_chain_scores_the_links_it_reached() -> None:
    score = score_run(record(truth_chain()), diagnosis(), tier="DEV")
    assert score.valid
    assert score.execution_witness is True
    assert score.effect_link is True
    assert score.propagation_link is None  # the chain has no propagation link
    assert (score.causes_named, score.causes_total) == (1, 1)
    assert (score.instances_named, score.instances_total) == (1, 1)
    assert score.false_strong_authority == 0 and not score.false_resolved
    assert score.time_to_diagnosis_seconds == 60.0 and score.reads == len(diagnosis().steps)


def test_a_strong_claim_on_an_actor_off_the_chain_is_false_strong_authority() -> None:
    elsewhere = Chain(
        links=(
            Link(
                role="cause",
                actor="shop/Deployment/somewhere-else",
                knowable=False,
                mechanism="config",
            ),
        )
    )
    score = score_run(record(elsewhere), diagnosis(), tier="DEV")
    assert score.false_strong_authority == 1
    assert score.causes_named == 0 and score.execution_witness is None


def test_a_negative_control_scores_abstention_and_counts_any_strong_claim_as_false() -> None:
    control = Chain(construction="no call edge from the faulted service to the symptom")
    score = score_run(record(control, family="negative-control"), diagnosis(), tier="HOLDOUT")
    assert score.abstained is False  # the engine named a cause and held a strong claim
    assert score.false_strong_authority == 1


def test_an_invalid_run_is_not_scored() -> None:
    bad = record(truth_chain()).model_copy(update={"invalid_reasons": ("missing recovery_at",)})
    score = score_run(bad, diagnosis(), tier="DEV")
    assert not score.valid and score.execution_witness is None and score.reads == 0


def test_the_aggregate_never_merges_tiers_and_skips_links_the_world_lacked() -> None:
    good = score_run(record(truth_chain()), diagnosis(), tier="DEV")
    invalid = RunScore(
        scenario_id="x", repeat=1, family="direct-pod-fault", tier="DEV", valid=False
    )
    holdout = good.model_copy(update={"tier": "HOLDOUT", "false_resolved": True})
    summary = aggregate([good, invalid, holdout])
    assert set(summary) == {"DEV/direct-pod-fault", "HOLDOUT/direct-pod-fault"}
    dev = summary["DEV/direct-pod-fault"]
    assert (dev["runs"], dev["valid_runs"]) == (2, 1)
    assert dev["execution_witness_recall"] == 1.0
    assert dev["propagation_link_recall"] is None
    assert summary["HOLDOUT/direct-pod-fault"]["false_resolved"] == 1
    assert dev["false_resolved"] == 0


def test_a_structural_path_is_not_an_execution_effect_or_propagation_witness() -> None:
    from packages.rca.causal_closure import EXECUTION_RULES

    plain = diagnosis()
    assert plain.resolution_trace is not None
    audits = tuple(
        audit.model_copy(
            update={
                "root_support": tuple(
                    r.model_copy(update={"rule_id": "m21.support.change-onset-path"})
                    if r.rule_id in EXECUTION_RULES
                    else r
                    for r in audit.root_support
                )
            }
        )
        for audit in plain.resolution_trace.hypothesis_audits
    )
    structural = plain.model_copy(
        update={
            "resolution_trace": plain.resolution_trace.model_copy(
                update={"hypothesis_audits": audits}
            )
        }
    )
    score = score_run(record(truth_chain()), structural, tier="DEV")
    assert score.execution_witness is False and score.effect_link is False


def test_the_links_of_a_fault_are_read_from_every_incident_of_the_run() -> None:
    nothing = diagnosis().model_copy(update={"resolution_trace": None})
    alone = score_run(record(truth_chain()), nothing, tier="DEV")
    together = score_run(record(truth_chain()), nothing, tier="DEV", also=[diagnosis()])
    assert alone.effect_link is False and together.effect_link is True


def test_a_propagation_link_that_cannot_yet_be_observed_is_not_measured() -> None:
    chain = truth_chain()
    with_propagation = Chain(
        links=(
            *chain.links,
            Link(
                role="propagation",
                actor="shop/Deployment/orders",
                knowable=False,
                mechanism="dependency latency",
                evidence_class="propagation",
            ),
        )
    )
    assert score_run(record(with_propagation), diagnosis(), tier="DEV").propagation_link is None


# ---- competing causes: every incident against its own symptom group (testbed contract §15) ----

OTHER = "shop/PodChaos/worker-kill"


def competing(
    groups: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...],
) -> Chain:
    from packages.evals.live.ground_truth import SymptomGroup

    base = truth_chain()
    kill = Link(role="cause", actor=OTHER, instance_uid="k1", knowable=True, mechanism="PodChaos")
    return Chain(
        links=(*base.links, kill),
        symptom_groups=tuple(SymptomGroup(alerts=a, causes=c, required=r) for a, c, r in groups),
    )


def test_each_incident_is_scored_against_its_own_group() -> None:
    chain = competing(
        (
            (("Latency",), (SCHEDULE,), (SCHEDULE,)),
            (("Lag",), (OTHER, SCHEDULE), (OTHER,)),
        )
    )
    d = diagnosis()
    score = score_run(
        record(chain, family="competing-causes"),
        d,
        tier="DEV",
        incidents=[("Latency", d), ("Lag", d), ("Unrelated", d)],
    )
    # the latency group is found; the lag group needs the pod-kill, which this diagnosis never names
    assert (score.groups_found, score.groups_total, score.unscored_incidents) == (1, 2, 1)
    assert (
        score.cross_attribution == 0
    )  # naming the delay for a lag incident is allowed (it contributes)
    assert (score.causes_named, score.causes_total) == (1, 2)


def test_naming_a_cause_outside_the_incidents_group_is_a_cross_attribution() -> None:
    chain = competing(
        (
            (("Lag",), (OTHER,), (OTHER,)),
            (("Latency",), (SCHEDULE,), (SCHEDULE,)),
        )
    )
    d = diagnosis()
    score = score_run(
        record(chain, family="competing-causes"), d, tier="DEV", incidents=[("Lag", d)]
    )
    assert score.cross_attribution == 1 and score.groups_found == 0
