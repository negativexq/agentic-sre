"""Bounded investigation frontier construction.

The frontier is structural bookkeeping, not a second causal ranking system.
Only actors with deterministic Findings enter the hypothesis resolver.  This
module names actors that are worth querying and records the bounded targets
that can provide evidence for them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256

from packages.rca.claims import admitted, symptom_links
from packages.rca.evidence_coverage import EvidenceCoverage, continuously_observed
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    FrontierAnswer,
    FrontierStatus,
    GapDimension,
    Hypothesis,
    InvestigationStatus,
    Lifecycle,
    ObjectVersion,
    StructuralAlternative,
)
from packages.rca.ranking import Context
from packages.rca.topology import WORKLOAD_KINDS, api_access_refs, config_refs, pod_spec_of

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
    "api_access": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
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


def _api_access(config: EntityRef, pod: EntityRef, context: Context) -> bool:
    """m21 §25: the Pod holds ``config`` only through its API access volume, and its controller never declares it."""
    version = context.topology.latest.get(pod)
    spec = pod_spec_of(version.body) if version is not None else None
    if pod.kind != "Pod" or spec is None or (config.kind, config.name) not in api_access_refs(spec):
        return False
    current = pod
    for _ in range(3):
        owners = context.topology.outgoing(current, "owned_by")
        if not owners:
            return False
        current = owners[0]
        owner = context.topology.latest.get(current)
        template = pod_spec_of(owner.body) if owner is not None else None
        if template is not None:
            return (config.kind, config.name) not in config_refs(template)
    return False


def _content(version: ObjectVersion) -> tuple[object, object]:
    return version.body.get("data"), version.body.get("binaryData")


def _changed(
    config: EntityRef,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    onset: datetime | None,
) -> bool:
    """An observed content change of ``config`` up to the onset (m21 §25, third bullet)."""
    versions = sorted(history.get(config, ()), key=lambda version: version.observed_at)
    return any(
        (later.lifecycle is Lifecycle.DELETED or _content(later) != _content(earlier))
        and (onset is None or later.observed_at <= onset)
        for earlier, later in zip(versions, versions[1:], strict=False)
    )


def derive_structural_frontier(
    context: Context,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]] | None = None,
    onset: datetime | None = None,
) -> tuple[StructuralAlternative, ...]:
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
        if (
            role == "configuration_source"
            and _api_access(actor, affected, context)
            and not _changed(actor, history or {}, onset)
        ):
            role = "api_access"
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


UNCHANGED_CONFIGURATION_RULE = "m21.frontier.unchanged-configuration"
ANSWERED_STATES = frozenset({"ANSWERED_ROLE_TRANSFERRED", "ANSWERED_NO_CHANGE_IN_WINDOW"})


def _write_record(body: Mapping[str, object]) -> datetime | None:
    """The latest of ``creationTimestamp`` and every ``managedFields`` time; None when any is missing."""
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return None
    entries = metadata.get("managedFields")
    created = metadata.get("creationTimestamp")
    if not isinstance(entries, list) or not entries or not isinstance(created, str):
        return None
    times = [created]
    for entry in entries:
        at = entry.get("time") if isinstance(entry, dict) else None
        if not isinstance(at, str):
            return None
        times.append(at)
    try:
        return max(datetime.fromisoformat(at.replace("Z", "+00:00")) for at in times)
    except ValueError:
        return None


def unchanged_configuration(
    alternative: StructuralAlternative,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    coverage: EvidenceCoverage | None,
    onset: datetime | None,
) -> FrontierAnswer | None:
    """m21 §26 B: a configuration whose last write precedes the window and that stayed unchanged to the onset.

    None when the rule cannot be assessed (not a configuration question, no coverage record, no onset): the
    question then keeps the answer it had. Otherwise the answer, ``OPEN`` with the failed condition named.
    """
    if alternative.role != "configuration_source" or coverage is None or onset is None:
        return None
    question = "DID_THE_CONFIGURATION_CHANGE_INSIDE_THE_WINDOW"

    def open_(reason: str, **fields: object) -> FrontierAnswer:
        return FrontierAnswer(
            alternative_id=alternative.alternative_id,
            question=question,
            state="OPEN",
            blocked_reason=reason,
            affected_claims=alternative.material_for_hypothesis_ids,
            remaining_uncertainty=("UPSTREAM_ROLE_UNDETERMINED",),
            rule_id=UNCHANGED_CONFIGURATION_RULE,
            **fields,  # type: ignore[arg-type]
        )

    versions = [
        version
        for version in sorted(
            history.get(alternative.actor, ()), key=lambda version: version.observed_at
        )
        if version.observed_at <= onset
    ]
    if not versions:
        return open_("NO_LISTING")
    listing = versions[-1]
    if listing.lifecycle is Lifecycle.DELETED:
        return open_("WRITTEN_IN_WINDOW")
    written = _write_record(listing.body)
    if written is None:
        return open_("NO_WRITE_RECORD", listed_at=listing.observed_at)
    if written >= coverage.starts_at:
        return open_("WRITTEN_IN_WINDOW", last_written_at=written, listed_at=listing.observed_at)
    gap = continuously_observed(
        coverage,
        alternative.actor.namespace or "",
        alternative.actor.kind,
        listing.observed_at,
        onset,
    )
    if gap is not None:
        return open_(gap, last_written_at=written, listed_at=listing.observed_at)
    return FrontierAnswer(
        alternative_id=alternative.alternative_id,
        question=question,
        state="ANSWERED_NO_CHANGE_IN_WINDOW",
        evidence_ids=(listing.evidence_id,),
        affected_claims=alternative.material_for_hypothesis_ids,
        remaining_uncertainty=("CONFIGURATION_MAY_REMAIN_A_CONDITION",),
        rule_id=UNCHANGED_CONFIGURATION_RULE,
        last_written_at=written,
        listed_at=listing.observed_at,
    )


def investigation_status(
    alternatives: Sequence[StructuralAlternative], *, bounded: bool
) -> InvestigationStatus:
    """Return the thin active-investigation status for a case."""
    if not alternatives and not bounded:
        return InvestigationStatus.NOT_REQUIRED
    if any(
        (item.answer is None or item.answer.state not in ANSWERED_STATES)
        and (item.status is FrontierStatus.UNEXPLORED or item.material_for_hypothesis_ids)
        for item in alternatives
    ):
        return InvestigationStatus.OPEN
    return InvestigationStatus.EXHAUSTED


__all__ = [
    "ANSWERED_STATES",
    "UNCHANGED_CONFIGURATION_RULE",
    "apply_frontier_progress",
    "covered_frontier_dimensions",
    "derive_structural_frontier",
    "investigation_status",
    "unchanged_configuration",
]
