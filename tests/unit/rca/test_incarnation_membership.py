"""m21 §22 Rule C: a claim's members belong to the actor's own Schedule incarnation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.hypotheses import group_candidates
from packages.rca.model import (
    Candidate,
    Edge,
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Symptoms,
)
from packages.rca.ranking import Context, RankingConfig
from packages.rca.topology import Topology

ONSET = datetime(2026, 1, 1, tzinfo=UTC)
SCHEDULE = EntityRef(kind="Schedule", namespace="lab", name="sched")
POD = EntityRef(kind="Pod", namespace="lab", name="web-0")


def _experiment(suffix: str) -> EntityRef:
    return EntityRef(kind="StressChaos", namespace="lab", name=f"sched-{suffix}")


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    uid: str | None,
    details: dict[str, object] | None = None,
    role: EvidenceTemporalRole = EvidenceTemporalRole.INITIATING,
) -> Finding:
    return Finding(
        entity=entity,
        kind=kind,
        at=ONSET - timedelta(seconds=30),
        entity_instance=EntityInstanceRef(entity=entity, uid=uid) if uid else None,
        incident_onset=ONSET,
        temporal_role=role,
        summary="observed",
        details=details or {},
        evidence_ids=(f"obs:{entity.name}:{uid}",),
    )


def _spawned(suffix: str, uid: str, schedule_uid: str | None) -> Candidate:
    """An experiment instance; ``schedule_uid`` is the incarnation a ``Spawned`` record names."""
    entity = _experiment(suffix)
    details: dict[str, object] = {"schedule": SCHEDULE.canonical}
    if schedule_uid is not None:
        details["schedule_uid"] = schedule_uid
    return Candidate(
        entity=entity,
        score=1,
        findings=(_finding(entity, FindingKind.FAULT_INJECTION, uid, details),),
    )


def _grouped(
    schedule_uids: tuple[str | None, ...], experiments: tuple[Candidate, ...]
) -> tuple[Hypothesis, ...]:
    edges = [Edge(source=SCHEDULE, relation="spawns", target=c.entity) for c in experiments]
    edges += [Edge(source=c.entity, relation="disrupts", target=POD) for c in experiments]
    topology = Topology(tuple(edges), {})
    symptoms = Symptoms(onset=ONSET, last_seen=ONSET, services=(), namespaces=(), alert_names=())
    context = Context(symptoms=symptoms, symptom_entities={POD}, topology=topology)
    schedule = Candidate(
        entity=SCHEDULE,
        score=1,
        findings=tuple(_finding(SCHEDULE, FindingKind.FAULT_SCHEDULE, u) for u in schedule_uids),
    )
    pod = Candidate(
        entity=POD,
        score=1,
        findings=(
            _finding(POD, FindingKind.FAILURE_EVENT, None, role=EvidenceTemporalRole.SUPPORTING),
        ),
    )
    return group_candidates(
        (schedule, *experiments, pod), topology, context, RankingConfig()
    ).hypotheses


def _claim(claims: tuple[Hypothesis, ...], actor: EntityRef, uid: str | None) -> Hypothesis:
    [found] = [
        h
        for h in claims
        if h.causal_actor == actor and (h.actor_instance.uid if h.actor_instance else None) == uid
    ]
    return found


def _sources(claim: Hypothesis) -> set[tuple[str, str | None]]:
    return {
        (f.entity.name, f.entity_instance.uid if f.entity_instance else None)
        for f in claim.findings
    }


TWO = (
    _spawned("aaaaa", "exp-1", "sched-1"),
    _spawned("bbbbb", "exp-2", "sched-2"),
)


def test_a_schedule_claim_keeps_only_its_own_spawned_experiments() -> None:
    claims = _grouped(("sched-1", "sched-2"), TWO)

    first = _claim(claims, SCHEDULE, "sched-1")
    assert _experiment("aaaaa") in first.members
    assert _experiment("bbbbb") not in first.members
    assert ("sched-bbbbb", "exp-2") not in _sources(first)
    second = _claim(claims, SCHEDULE, "sched-2")
    assert _experiment("bbbbb") in second.members
    assert _experiment("aaaaa") not in second.members


def test_an_experiment_claim_keeps_neither_the_other_incarnation_nor_its_experiments() -> None:
    claims = _grouped(("sched-1", "sched-2"), TWO)

    experiment = _claim(claims, _experiment("aaaaa"), "exp-1")
    assert ("sched-bbbbb", "exp-2") not in _sources(experiment)
    assert ("sched", "sched-2") not in _sources(experiment)
    assert ("sched", "sched-1") in _sources(experiment)


def test_an_experiment_without_a_spawned_record_joins_the_only_incarnation() -> None:
    unbound = _spawned("ccccc", "exp-3", None)
    claims = _grouped(("sched-1",), (TWO[0], unbound))

    assert _experiment("ccccc") in _claim(claims, SCHEDULE, "sched-1").members


def test_an_experiment_without_a_spawned_record_joins_neither_of_two_incarnations() -> None:
    unbound = _spawned("ccccc", "exp-3", None)
    claims = _grouped(("sched-1", "sched-2"), (*TWO, unbound))

    for uid in ("sched-1", "sched-2"):
        assert _experiment("ccccc") not in _claim(claims, SCHEDULE, uid).members


def test_an_actor_of_unknown_incarnation_keeps_its_members() -> None:
    claims = _grouped((None,), TWO)

    schedule = _claim(claims, SCHEDULE, None)
    assert {_experiment("aaaaa"), _experiment("bbbbb")} <= set(schedule.members)


def test_a_non_chaos_actor_keeps_both_incarnations() -> None:
    claims = _grouped(("sched-1", "sched-2"), TWO)

    pod = _claim(claims, POD, None)
    assert {_experiment("aaaaa"), _experiment("bbbbb")} <= set(pod.members)
