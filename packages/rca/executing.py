"""The executing instance beside the root cause (m21 contract §17).

Presentation, outside the epistemic digest: for each actor shown, what its fired execution witnesses say ran. A
structural path, a D1 support or a name never supplies one; an actor with no fired execution witness has none.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from packages.rca.causal_closure import (
    EXECUTION_RULE,
    EXECUTION_RULES,
    FAILED_ROLLOUT_RULE,
    FAULT_EXECUTION_RULE,
    ROLLOUT_EXECUTION_RULE,
)
from packages.rca.model import (
    CausalWitness,
    EntityRef,
    ExecutingInstance,
    ObjectVersion,
    ResolutionTrace,
    RootSupportStatus,
)


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _hop_target(witness: CausalWitness, relation: str) -> EntityRef | None:
    return next((hop.target for hop in witness.path if hop.relation == relation), None)


def _fault(witness: CausalWitness) -> ExecutingInstance:
    origin = witness.origin
    target = _hop_target(witness, "fault_targets") or (
        witness.symptom if witness.symptom.kind == "Pod" else None
    )
    started = ended = None
    for item in origin.details.get("execution_targets", ()):
        namespace, _, rest = str(item.get("target", "")).partition("/")
        if target is not None and (namespace, rest.partition("/")[0]) == (
            target.namespace,
            target.name,
        ):
            started, ended = _instant(item.get("applied_at")), _instant(item.get("recovered_at"))
            break
    return ExecutingInstance(
        actor=witness.actor,
        instance=origin.entity,
        instance_uid=origin.entity_instance.uid if origin.entity_instance is not None else None,
        target=target,
        started_at=started,
        ended_at=ended,
        rule_id=FAULT_EXECUTION_RULE,
        evidence_ids=witness.evidence_ids,
    )


def _owner_uid(version: ObjectVersion) -> str | None:
    metadata = version.body.get("metadata")
    owners = metadata.get("ownerReferences") if isinstance(metadata, dict) else None
    for owner in owners if isinstance(owners, list) else ():
        if isinstance(owner, dict) and owner.get("uid"):
            return str(owner["uid"])
    return None


def _rollout(
    witness: CausalWitness, rule_id: str, history: Mapping[EntityRef, Sequence[ObjectVersion]]
) -> ExecutingInstance | None:
    pod = _hop_target(witness, "rolls_out")
    if pod is None:
        return None
    versions = history.get(pod, ())
    created = next((v for v in versions if v.lifecycle.value == "CREATED"), None)
    deleted = next((v for v in versions if v.lifecycle.value == "DELETED"), None)
    owner = _owner_uid(created) if created is not None else None
    replicaset = next(
        (
            ref
            for ref, rs_versions in history.items()
            if ref.kind == "ReplicaSet"
            and ref.namespace == pod.namespace
            and owner is not None
            and any(v.instance_uid == owner for v in rs_versions)
        ),
        None,
    )
    if replicaset is None:
        return None  # the pod's ReplicaSet is not in the evidence: name nothing rather than guess
    return ExecutingInstance(
        actor=witness.actor,
        instance=replicaset,
        instance_uid=owner,
        target=pod,
        started_at=created.observed_at if created is not None else None,
        ended_at=deleted.observed_at if deleted is not None else None,
        rule_id=rule_id,
        evidence_ids=witness.evidence_ids,
    )


def executing_instances(
    trace: ResolutionTrace | None,
    actors: Collection[EntityRef],
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> tuple[ExecutingInstance, ...]:
    """What ran for ``actors``, from the fired execution witnesses of ``trace`` only."""
    if trace is None:
        return ()
    found: dict[tuple[EntityRef, EntityRef, EntityRef | None], ExecutingInstance] = {}
    for audit in trace.hypothesis_audits:
        for record in audit.root_support:
            if (
                record.status is not RootSupportStatus.FIRED
                or record.rule_id not in EXECUTION_RULES
            ):
                continue
            for witness in record.witnesses:
                if witness.actor not in actors:
                    continue
                instance: ExecutingInstance | None
                if record.rule_id == FAULT_EXECUTION_RULE:
                    instance = _fault(witness)
                elif record.rule_id in (ROLLOUT_EXECUTION_RULE, FAILED_ROLLOUT_RULE):
                    instance = _rollout(witness, record.rule_id, history)
                elif record.rule_id == EXECUTION_RULE:
                    instance = ExecutingInstance(
                        actor=witness.actor,
                        instance=witness.symptom,
                        rule_id=EXECUTION_RULE,
                        evidence_ids=witness.evidence_ids,
                    )
                else:
                    instance = None
                if instance is not None:
                    found.setdefault((instance.actor, instance.instance, instance.target), instance)
    return tuple(
        sorted(
            found.values(),
            key=lambda item: (
                item.actor.canonical,
                item.started_at.isoformat() if item.started_at else "",
                item.instance.canonical,
                item.target.canonical if item.target else "",
            ),
        )
    )
