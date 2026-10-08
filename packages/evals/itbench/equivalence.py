"""Evidence-backed causal equivalence for ITBench-Lite (roadmap C11; docs/architecture/c11-itbench-equivalence.md).

The engine names the actor that controls a fault (a chaos ``Schedule``); ITBench labels the experiment instance that
Schedule spawned. This module never changes the answer. It records, at prediction time and from the snapshot alone,
which experiment instances the predicted root cause is *proven* to stand behind, and at grading time scores tracks
that are kept apart from the exact score:

- ``exact``: the root cause alone, the existing grader (unchanged, comparable with earlier runs);
- ``controller_record``: the root cause, or an experiment its exact Schedule instance named in a ``Spawned``
  controller record, bound to one experiment UID (structural evidence: the controller created it);
- ``controller_execution``: as above, and that experiment's own controller reported applying the fault to a pod
  before the incident's reference time (execution evidence, as reported by the controller);
- ``executing_instance``: the root cause, or an instance the engine's fired execution witnesses name
  (``Diagnosis.executing_instances``, m21 §17).

Each track scores one answer: the equivalence class counts as a single prediction, so naming many experiments can
never add true positives. Ground truth is read only by :func:`grade_tracks`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntityPrediction,
    ITBenchGroundTruth,
    parse_canonical_entity,
)
from packages.evals.itbench.grader import grade_root_cause_entities
from packages.rca.fault_execution import fault_executions
from packages.rca.model import ClusterEvent, Diagnosis

RELATIONS_VERSION = "c11.v1"
TRACKS = ("exact", "controller_record", "controller_execution", "executing_instance")


class ControlledInstance(BaseModel):
    """One experiment instance the root cause's exact Schedule instance created, with its evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str
    uid: str
    spawn_evidence_ids: tuple[str, ...]
    # Controller-reported applications to a pod that began at or before the incident's reference time.
    execution_evidence_ids: tuple[str, ...] = ()
    applied_at: datetime | None = None
    targets: tuple[str, ...] = ()

    @property
    def executed(self) -> bool:
        return bool(self.execution_evidence_ids)


class CausalRelations(BaseModel):
    """What the predicted root cause provably stands behind, derived without ground truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = RELATIONS_VERSION
    root_cause: str | None
    root_uid: str | None
    reference_time: datetime | None
    instances: tuple[ControlledInstance, ...] = ()
    # Why nothing (or less) was recorded: NO_ROOT_CAUSE, NOT_A_SCHEDULE, ROOT_INSTANCE_UNKNOWN,
    # SCHEDULE_INSTANCE_NOT_OBSERVED, CHILD_UID_UNKNOWN:<name>, AMBIGUOUS_INCARNATIONS:<name>.
    excluded: tuple[str, ...] = ()


def controller_relations(diagnosis: Diagnosis, events: Sequence[ClusterEvent]) -> CausalRelations:
    """Prediction side: the experiments the root cause's Schedule *instance* created and, separately, executed.

    The link is the controller's own ``Spawned`` record of that Schedule UID naming the experiment, and the
    experiment's events reporting exactly one UID under that name in the namespace. A name that matches no
    record, a second incarnation of the same name, or a Schedule instance other than the root cause's
    is never linked. No name similarity, namespace or timing is used to create a link.
    """
    root = diagnosis.root_cause
    reference = diagnosis.symptoms.reference_time
    hypothesis = diagnosis.hypothesis
    instance = (
        hypothesis.actor_instance
        if hypothesis is not None and hypothesis.causal_actor == root
        else None
    )
    base: dict[str, Any] = {
        "root_cause": root.canonical if root else None,
        "root_uid": instance.uid if instance else None,
        "reference_time": reference,
    }
    if root is None:
        return CausalRelations(**base, excluded=("NO_ROOT_CAUSE",))
    if root.kind != "Schedule":
        return CausalRelations(**base, excluded=("NOT_A_SCHEDULE",))
    if instance is None:
        return CausalRelations(**base, excluded=("ROOT_INSTANCE_UNKNOWN",))
    executions = fault_executions(events)
    schedule = next(
        (x for x in executions if x.ref.entity == root and x.ref.uid == instance.uid), None
    )
    if schedule is None:
        return CausalRelations(**base, excluded=("SCHEDULE_INSTANCE_NOT_OBSERVED",))
    incarnations: dict[str, set[str | None]] = defaultdict(set)
    for execution in executions:
        if execution.ref.entity.kind != "Schedule":
            incarnations[execution.ref.entity.canonical].add(execution.ref.uid)
    by_ref = {(x.ref.entity, x.ref.uid): x for x in executions}
    instances: list[ControlledInstance] = []
    excluded: list[str] = []
    for child in schedule.children:
        name = child.entity.canonical
        if child.uid is None:
            excluded.append(f"CHILD_UID_UNKNOWN:{name}")
            continue
        if len(incarnations[name]) != 1:
            excluded.append(f"AMBIGUOUS_INCARNATIONS:{name}")
            continue
        execution = by_ref[(child.entity, child.uid)]
        if execution.schedule is None or execution.schedule.uid != instance.uid:
            continue  # another Schedule instance's record claimed it first
        applied = [
            t
            for t in execution.targets
            if t.applied_at is not None and reference is not None and t.applied_at <= reference
        ]
        instances.append(
            ControlledInstance(
                entity=name,
                uid=child.uid,
                spawn_evidence_ids=execution.spawn_evidence,
                execution_evidence_ids=tuple(e for t in applied for e in t.applied_evidence),
                applied_at=min((t.applied_at for t in applied if t.applied_at), default=None),
                targets=tuple(sorted({t.pod.canonical for t in applied})),
            )
        )
    return CausalRelations(
        **base,
        instances=tuple(sorted(instances, key=lambda i: (i.entity, i.uid))),
        excluded=tuple(dict.fromkeys(excluded)),
    )


def track_members(
    diagnosis: Diagnosis, relations: CausalRelations | None
) -> dict[str, tuple[str, ...]]:
    """The entities each track accepts as the one answer, root cause first."""
    root = diagnosis.root_cause.canonical if diagnosis.root_cause else None
    if root is None:
        return {track: () for track in TRACKS}
    linked = relations.instances if relations and relations.root_cause == root else ()
    executing = tuple(
        dict.fromkeys(
            i.instance.canonical for i in diagnosis.executing_instances if i.actor.canonical == root
        )
    )
    return {
        "exact": (root,),
        "controller_record": (root, *(i.entity for i in linked)),
        "controller_execution": (root, *(i.entity for i in linked if i.executed)),
        "executing_instance": (root, *executing),
    }


def _grade_class(
    scenario_id: str, members: tuple[str, ...], truth: ITBenchGroundTruth
) -> dict[str, Any]:
    """One answer: true positive if any member matches a root group, counted once."""
    if not members:
        return {"correct": False, "matched_by": None, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    matched_by = None
    for member in members:
        output = ITBenchAgentOutput(
            incident_id=scenario_id,
            scenario_id=scenario_id,
            contributing_factor=(
                ITBenchEntityPrediction(
                    entity=parse_canonical_entity(member), rank=1, condition="class"
                ),
            ),
            native_terminal="class",
        )
        if grade_root_cause_entities(output, truth).true_positive > 0:
            matched_by = member
            break
    roots = sum(1 for g in truth.root_cause_groups if g.root_cause)
    tp = 1 if matched_by is not None else 0
    precision = float(tp)  # one prediction
    recall = tp / roots if roots else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "correct": bool(tp),
        "matched_by": matched_by,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def grade_tracks(
    scenario_id: str,
    diagnosis: Diagnosis,
    relations: CausalRelations | None,
    truth: ITBenchGroundTruth,
) -> dict[str, dict[str, Any]]:
    """Grading side: each track scored separately; the exact track is the existing grader's answer."""
    members = track_members(diagnosis, relations)
    result = {track: _grade_class(scenario_id, members[track], truth) for track in TRACKS}
    for track in TRACKS:
        result[track]["members"] = len(members[track])
    return result


__all__ = [
    "RELATIONS_VERSION",
    "TRACKS",
    "CausalRelations",
    "ControlledInstance",
    "controller_relations",
    "grade_tracks",
    "track_members",
]
