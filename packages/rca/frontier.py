"""Bounded investigation frontier construction.

The frontier is structural bookkeeping, not a second causal ranking system.
Only actors with deterministic Findings enter the hypothesis resolver.  This
module names actors that are worth querying and records the bounded targets
that can provide evidence for them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256

from packages.rca.claims import admitted, symptom_links
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    FrontierStatus,
    GapDimension,
    Hypothesis,
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
    "network_policy": (GapDimension.CHANGE_TIMING,),
    "fault_actor": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    "dependency": (GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
    "workload_controller": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "quota": (GapDimension.EVENT_SEQUENCE, GapDimension.ENTITY_STATE),
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


def derive_structural_frontier(context: Context) -> tuple[StructuralAlternative, ...]:
    """Derive role-constrained alternatives from direct causal topology.

    The relation itself is the structural basis.  We do not scan arbitrary
    max-depth paths and do not assign evidence, score, or a causal hypothesis
    to the resulting actor.
    """
    workloads = _workloads(context)
    rows: dict[tuple[EntityRef, str], tuple[set[EntityRef], tuple[str, ...]]] = {}

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
        rows[key] = (
            {affected} | (previous[0] if previous else set()),
            tuple(sorted(set((*(previous[1] if previous else ()), *basis)))),
        )

    # ResourceQuota with pod admission limits is an explicit namespace-wide
    # admission mechanism. Its presence creates a question, never admission or
    # support. Scope/selectors remain unanswered until an actual rejection.
    for actor, version in context.topology.latest.items():
        if actor.kind != "ResourceQuota":
            continue
        spec, status = version.body.get("spec", {}), version.body.get("status", {})
        hard = (spec.get("hard", {}) if isinstance(spec, dict) else {}) or (
            status.get("hard", {}) if isinstance(status, dict) else {}
        )
        if not isinstance(hard, dict) or not any(
            k in {"pods", "cpu", "memory", "ephemeral-storage"}
            or k.startswith(("requests.", "limits."))
            for k in hard
        ):
            continue
        quota_targets = {w for w in workloads if w.namespace == actor.namespace}
        if quota_targets:
            rows[(actor, "quota")] = (quota_targets, ("declared:pod-admission-quota",))

    # A directly alerting workload/controller is a legitimate structural actor
    # even when no historical change has been consumed yet.
    for workload in sorted(workloads, key=lambda item: item.canonical):
        key = (workload, "workload_controller")
        rows.setdefault(key, ({workload}, ("symptom:workload_controller",)))

    alternatives: list[StructuralAlternative] = []
    for (actor, role), (affected_entities, basis) in sorted(
        rows.items(), key=lambda item: (item[0][0].canonical, item[0][1])
    ):
        path = context.topology.causal_path(actor, set(context.symptom_entities), max_depth=4) or ()
        alternatives.append(
            StructuralAlternative(
                alternative_id=_alternative_id(actor, role, basis),
                affected_entities=tuple(sorted(affected_entities, key=lambda item: item.canonical)),
                actor=actor,
                role=role,
                structural_basis=basis,
                causal_path=path,
                linked_symptoms=_linked_symptoms(actor, context),
                queryable_dimensions=_DIMENSIONS_BY_ROLE[role],
                observation_targets=tuple(
                    sorted(
                        {
                            target
                            for affected in affected_entities
                            for target in _related_targets(actor, affected, context)
                        },
                        key=lambda item: item.canonical,
                    )
                ),
                queried_dimensions=(),
                status=FrontierStatus.UNEXPLORED,
            )
        )
    return tuple(alternatives)


def covered_frontier_dimensions(
    diagnosis: Diagnosis,
    *,
    capability: str,
    target: EntityRef,
    evidence_acquired: bool = True,
) -> dict[str, tuple[GapDimension, ...]]:
    """Return structural dimensions covered by one authorized telemetry read."""
    if not evidence_acquired or capability == "runtime_traces":
        return {}
    covered: dict[str, set[GapDimension]] = {}
    for gap in diagnosis.information_gaps:
        if any(
            query.capability == capability and query.target == target
            for query in gap.authorized_queries
        ):
            for query in gap.authorized_queries:
                if query.capability == capability and query.target == target:
                    for alternative_id in query.alternative_ids:
                        covered.setdefault(alternative_id, set()).add(gap.dimension)
    return {
        alternative_id: tuple(sorted(dimensions, key=lambda item: item.value))
        for alternative_id, dimensions in sorted(covered.items())
    }


def apply_frontier_progress(
    alternatives: Sequence[StructuralAlternative],
    *,
    hypotheses: Sequence[Hypothesis],
    queried_dimensions_by_alternative: Mapping[str, Sequence[GapDimension]],
) -> tuple[StructuralAlternative, ...]:
    """Apply one authoritative, evidence-backed frontier lifecycle."""
    promoted_actors = {
        entity
        for hypothesis in hypotheses
        if admitted(hypothesis)
        for entity in (hypothesis.causal_actor,)
    }
    updated: list[StructuralAlternative] = []
    for alternative in alternatives:
        queried = set(alternative.queried_dimensions)
        queried.update(queried_dimensions_by_alternative.get(alternative.alternative_id, ()))
        ordered_queried = tuple(sorted(queried, key=lambda item: item.value))
        if alternative.actor in promoted_actors:
            status = FrontierStatus.PROMOTED
        elif set(alternative.queryable_dimensions).issubset(queried):
            status = FrontierStatus.QUERIED_NO_CAUSAL_FINDING
        else:
            status = FrontierStatus.UNEXPLORED
        updated.append(
            alternative.model_copy(update={"queried_dimensions": ordered_queried, "status": status})
        )
    return tuple(updated)


def material_frontier(
    alternatives: Sequence[StructuralAlternative],
    hypotheses: Sequence[Hypothesis],
) -> tuple[StructuralAlternative, ...]:
    """Bind unknown upstream mechanisms to concrete incident-linked claims.

    Query completion, no findings, promotion and budget exhaustion are not
    negative causal evidence. None can close this boundary. A modeled upstream
    dependency or configuration of an observed chain limits initiating certainty.
    """
    result: list[StructuralAlternative] = []
    for alternative in alternatives:
        affected_ids = []
        if alternative.role not in {
            "dependency",
            "configuration_source",
            "autoscaler",
            "network_policy",
            "fault_actor",
            "quota",
        }:
            continue
        for hypothesis in hypotheses:
            if not admitted(hypothesis) or hypothesis.causal_actor == alternative.actor:
                continue
            chain = {hypothesis.causal_actor}
            for symptom, path in symptom_links(hypothesis):
                chain.add(symptom)
                chain.update(hop.source for hop in path)
                chain.update(hop.target for hop in path)
            if chain.intersection(alternative.affected_entities):
                affected_ids.append(hypothesis.hypothesis_id)
        if affected_ids:
            result.append(
                alternative.model_copy(
                    update={
                        "material_for_hypothesis_ids": tuple(sorted(affected_ids)),
                        "missing_decision": "UPSTREAM_MECHANISM_COULD_CHANGE_INITIATING_CAUSE",
                    }
                )
            )
    return tuple(sorted(result, key=lambda item: item.alternative_id))


def investigation_status(
    alternatives: Sequence[StructuralAlternative], *, bounded: bool
) -> InvestigationStatus:
    """Return the thin active-investigation status for a case."""
    if not alternatives and not bounded:
        return InvestigationStatus.NOT_REQUIRED
    if any(
        (item.answer is None or item.answer.state != "ANSWERED_ROLE_TRANSFERRED")
        and (item.status is FrontierStatus.UNEXPLORED or item.material_for_hypothesis_ids)
        for item in alternatives
    ):
        return InvestigationStatus.OPEN
    return InvestigationStatus.EXHAUSTED


__all__ = [
    "apply_frontier_progress",
    "covered_frontier_dimensions",
    "derive_structural_frontier",
    "investigation_status",
]
