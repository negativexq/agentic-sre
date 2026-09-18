"""Bounded investigation frontier construction.

The frontier is structural bookkeeping, not a second causal ranking system.
Only actors with deterministic Findings enter the hypothesis resolver.  This
module names actors that are worth querying and records the bounded targets
that can provide evidence for them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from hashlib import sha256

from packages.rca.model import (
    Candidate,
    EntityRef,
    FrontierStatus,
    GapDimension,
    InvestigationStatus,
    StructuralAlternative,
)
from packages.rca.ranking import Context
from packages.rca.topology import WORKLOAD_KINDS

_ROLE_BY_RELATION = {
    "uses_config": "configuration_source",
    "scales": "autoscaler",
    "restricts": "network_policy",
    "disrupts": "fault_actor",
    "calls": "dependency",
    "owned_by": "workload_controller",
}

_DIMENSIONS_BY_ROLE = {
    "configuration_source": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "autoscaler": (GapDimension.AUTOSCALING_TARGET_STATE, GapDimension.EVENT_SEQUENCE),
    "network_policy": (GapDimension.TOPOLOGY_RELATION, GapDimension.ENTITY_STATE),
    "fault_actor": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    "dependency": (GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
    "workload_controller": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
}


def _workloads(context: Context) -> set[EntityRef]:
    result: set[EntityRef] = set()
    for entity in context.symptom_entities:
        if entity.kind in WORKLOAD_KINDS:
            result.add(entity)
        workload = context.topology.workload_of(entity)
        if workload is not None:
            result.add(workload)
        for neighbor in context.topology.outgoing(entity, "routes_to"):
            if neighbor.kind in WORKLOAD_KINDS:
                result.add(neighbor)
    return result


def _related_targets(
    actor: EntityRef, affected: EntityRef, context: Context
) -> tuple[EntityRef, ...]:
    targets = {actor, affected}
    workload = context.topology.workload_of(affected) or (
        affected if affected.kind in WORKLOAD_KINDS else None
    )
    if workload is not None:
        targets.add(workload)
        targets.update(
            entity
            for entity in context.topology.latest
            if entity.kind == "Pod" and context.topology.workload_of(entity) == workload
        )
    return tuple(sorted(targets, key=lambda item: item.canonical))


def _alternative_id(actor: EntityRef, role: str, basis: Sequence[str]) -> str:
    payload = {
        "actor": actor.canonical,
        "role": role,
        "basis": sorted(basis),
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    return f"alternative:{digest}"


def _linked_symptoms(actor: EntityRef, context: Context) -> tuple[str, ...]:
    reached = context.topology.causal_reachable(actor, max_depth=4)
    return tuple(
        sorted(
            entity.canonical
            for entity in context.symptom_entities
            if entity == actor or entity in reached
        )
    )[:8]


def derive_structural_frontier(
    context: Context, existing: Iterable[Candidate] = ()
) -> tuple[StructuralAlternative, ...]:
    """Derive role-constrained alternatives from direct causal topology.

    The relation itself is the structural basis.  We do not scan arbitrary
    max-depth paths and do not assign evidence, score, or a causal hypothesis
    to the resulting actor.
    """
    evidence_actors = {candidate.entity for candidate in existing if candidate.findings}
    workloads = _workloads(context)
    rows: dict[tuple[EntityRef, str], tuple[EntityRef, tuple[str, ...]]] = {}

    for edge in context.topology.edges:
        role = _ROLE_BY_RELATION.get(edge.relation)
        if role is None:
            continue
        actor, affected = edge.target, edge.source
        if edge.relation in {"scales", "restricts", "disrupts"}:
            actor, affected = edge.source, edge.target
        elif edge.relation == "calls":
            actor, affected = edge.target, edge.source
        elif edge.relation == "owned_by":
            actor, affected = edge.target, edge.source

        # ReplicaSets and Pods are runtime manifestations.  Their owner
        # relation may expose a real Deployment actor, but the child itself
        # must remain an observation target rather than a causal alternative.
        if role == "workload_controller" and actor.kind not in WORKLOAD_KINDS:
            continue

        affected_workload = context.topology.workload_of(affected) or (
            affected if affected.kind in WORKLOAD_KINDS else None
        )
        if affected_workload not in workloads and affected not in context.symptom_entities:
            continue
        if actor.kind in {"Pod", "Service"} and role not in {"dependency"}:
            continue
        basis: tuple[str, ...] = (f"{edge.relation}:{actor.kind}:{affected.kind}",)
        key = (actor, role)
        previous = rows.get(key)
        rows[key] = (affected, tuple(sorted(set((*(previous[1] if previous else ()), *basis)))))

    # A directly alerting workload/controller is a legitimate structural actor
    # even when no historical change has been consumed yet.
    for workload in sorted(workloads, key=lambda item: item.canonical):
        key = (workload, "workload_controller")
        rows.setdefault(key, (workload, ("symptom:workload_controller",)))

    alternatives: list[StructuralAlternative] = []
    for (actor, role), (affected, basis) in sorted(
        rows.items(), key=lambda item: (item[0][0].canonical, item[0][1])
    ):
        path = context.topology.causal_path(actor, set(context.symptom_entities), max_depth=4) or ()
        alternatives.append(
            StructuralAlternative(
                alternative_id=_alternative_id(actor, role, basis),
                actor=actor,
                role=role,
                structural_basis=basis,
                causal_path=path,
                linked_symptoms=_linked_symptoms(actor, context),
                queryable_dimensions=_DIMENSIONS_BY_ROLE[role],
                observation_targets=_related_targets(actor, affected, context),
                status=(
                    FrontierStatus.PROMOTED
                    if actor in evidence_actors
                    else FrontierStatus.UNEXPLORED
                ),
            )
        )
    return tuple(alternatives)


def investigation_status(
    alternatives: Sequence[StructuralAlternative], *, bounded: bool
) -> InvestigationStatus:
    """Return the thin active-investigation status for a case."""
    if not bounded:
        return InvestigationStatus.NOT_REQUIRED
    if any(item.status is FrontierStatus.UNEXPLORED for item in alternatives):
        return InvestigationStatus.OPEN
    return InvestigationStatus.EXHAUSTED


__all__ = ["derive_structural_frontier", "investigation_status"]
