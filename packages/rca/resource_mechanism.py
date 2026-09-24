"""Rule ``m16.resource-pressure.v1`` (contract ``m16.v1-a2``, section 18).

A hypothesis whose only initiating premise is a workload change that lowered
(or newly set) container CPU/memory limits requires resource pressure on the
Pods running the new template. When every such Pod is measured normal for
every lowered resource across the incident window, and no positive pressure
evidence exists, that required mechanism is positively contradicted.

Missing, partial or unknown measurements never contradict anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from packages.rca.json_access import child, mapping
from packages.rca.model import (
    EliminationPrecondition,
    EntityRef,
    Finding,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    ResourcePressure,
)
from packages.rca.signals import PRESSURE_THRESHOLD, parse_quantity

RULE_ID = "m16.resource-pressure"
RULE_VERSION = "v1"
_RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
# The existing Prometheus resource template uses a fixed 15-second step.
_STEP = timedelta(seconds=15)
_BASELINE_GAP = timedelta(minutes=5)

PressureReader = Callable[[Sequence[EntityRef], datetime], Sequence[ResourcePressure]]


@dataclass(frozen=True)
class PodCoverage:
    pod: EntityRef
    container: str
    resource: str
    window_start: datetime
    window_end: datetime
    peak: float
    evidence_id: str


@dataclass(frozen=True)
class MechanismMismatch:
    """A passed rule evaluation, with everything its audit record needs."""

    hypothesis_id: str
    actor: EntityRef
    lowered: tuple[tuple[str, str], ...]
    pods: tuple[EntityRef, ...]
    onset: datetime
    boundary: datetime
    window_start: datetime
    window_end: datetime
    coverage: tuple[PodCoverage, ...]
    change_evidence_ids: tuple[str, ...]
    preconditions: tuple[EliminationPrecondition, ...]


def _containers(spec_holder: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    template = child(child(spec_holder, "spec"), "template")
    return {
        str(item.get("name")): item
        for item in (child(template, "spec").get("containers") or [])
        if isinstance(item, dict) and item.get("name")
    }


def _without_resources(body: Mapping[str, Any]) -> str:
    """Desired state with container resources and the restart annotation removed."""
    spec = json.loads(json.dumps(child(body, "spec"), default=str))
    template = mapping(spec.get("template"))
    for item in child(template, "spec").get("containers") or []:
        if isinstance(item, dict):
            item.pop("resources", None)
    annotations = child(child(template, "metadata"), "annotations")
    annotations.pop(_RESTART_ANNOTATION, None)
    labels = child(body, "metadata").get("labels")
    return json.dumps({"spec": spec, "labels": labels}, sort_keys=True, default=str)


def _lowered_limits(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    old, new = _containers(before), _containers(after)
    lowered: list[tuple[str, str]] = []
    for name, container in new.items():
        new_limits = child(child(container, "resources"), "limits")
        old_limits = child(child(old.get(name) or {}, "resources"), "limits")
        for resource in ("memory", "cpu"):
            now = parse_quantity(new_limits.get(resource))
            was = parse_quantity(old_limits.get(resource))
            if now is not None and (was is None or now < was):
                lowered.append((name, resource))
    return lowered


def _change_versions(
    finding: Finding, versions: Sequence[ObjectVersion]
) -> tuple[ObjectVersion, ObjectVersion] | None:
    if len(finding.evidence_ids) != 2:
        return None
    previous_id, current_id = finding.evidence_ids
    for older, newer in zip(versions, versions[1:], strict=False):
        if older.evidence_id == previous_id and newer.evidence_id == current_id:
            return older, newer
    return None


def _created(versions: Sequence[ObjectVersion]) -> datetime | None:
    raw = child(versions[0].body, "metadata").get("creationTimestamp") if versions else None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _owner(version: ObjectVersion) -> tuple[str, str] | None:
    for ref in child(version.body, "metadata").get("ownerReferences") or []:
        item = mapping(ref)
        if item.get("controller") and isinstance(item.get("name"), str):
            return str(item.get("kind")), str(item["name"])
    return None


def _bound_pods(
    actor: EntityRef,
    template_containers: Mapping[str, Any],
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[EntityRef, ...] | None:
    """Pods of the actor running exactly the post-change containers during the window.

    v1 binds Deployment Pods through the ReplicaSet whose template matches the
    post-change containers. Other workload kinds are not positively bindable
    here, so the rule stays inapplicable for them.
    """
    if actor.kind != "Deployment":
        return None
    replica_sets = {
        entity.name
        for entity, versions in history.items()
        if entity.kind == "ReplicaSet"
        and entity.namespace == actor.namespace
        and versions
        and _owner(versions[-1]) == (actor.kind, actor.name)
        and _containers(versions[-1].body) == template_containers
    }
    pods: list[EntityRef] = []
    for entity, versions in history.items():
        if entity.kind != "Pod" or entity.namespace != actor.namespace or not versions:
            continue
        owner = _owner(versions[-1])
        if owner is None or owner[0] != "ReplicaSet" or owner[1] not in replica_sets:
            continue
        created = _created(versions)
        if created is None:
            return None
        deleted = next((v.observed_at for v in versions if v.lifecycle is Lifecycle.DELETED), None)
        if created <= window_end and (deleted is None or deleted >= window_start):
            pods.append(entity)
    return tuple(sorted(pods, key=lambda item: item.canonical)) or None


def _oom_or_eviction(
    pods: Sequence[EntityRef],
    findings: Sequence[Finding],
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> bool:
    bound = set(pods)
    for finding in findings:
        if finding.entity not in bound:
            continue
        if finding.kind is FindingKind.RESOURCE_PRESSURE:
            return True
        if finding.kind is FindingKind.FAILURE_EVENT and finding.details.get("reason") == "Evicted":
            return True
        if finding.kind is FindingKind.CONTAINER_FAILURE and "OOMKilled" in json.dumps(
            finding.details, default=str
        ):
            return True
    for pod in pods:
        for version in history.get(pod, ()):
            if "OOMKilled" in json.dumps(child(version.body, "status"), default=str):
                return True
    return False


def assess_resource_mechanism(
    hypothesis: Hypothesis,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    findings: Sequence[Finding],
    read_pressure: PressureReader,
    onset: datetime | None,
    grace: timedelta,
) -> MechanismMismatch | None:
    """Return the mismatch only when every precondition passes."""
    actor = hypothesis.causal_actor
    initiating = hypothesis.initiating_findings
    if onset is None or actor.kind != "Deployment" or not initiating:
        return None
    # R1: every initiating premise is a spec change on the actor itself.
    if any(f.kind is not FindingKind.SPEC_CHANGE or f.entity != actor for f in initiating):
        return None
    versions = history.get(actor, ())
    lowered: list[tuple[str, str]] = []
    change_times: list[datetime] = []
    latest_after: ObjectVersion | None = None
    for finding in initiating:
        pair = _change_versions(finding, versions)
        if pair is None:
            return None
        before, after = pair
        # R2: nothing but container resources changed.
        if _without_resources(before.body) != _without_resources(after.body):
            return None
        # R3: a memory/CPU limit was lowered or newly set.
        found = _lowered_limits(before.body, after.body)
        if not found:
            return None
        lowered.extend(found)
        change_times.append(after.observed_at)
        if latest_after is None or after.observed_at > latest_after.observed_at:
            latest_after = after
    assert latest_after is not None
    lowered = sorted(set(lowered))
    boundary = onset + grace
    window_start = max(max(change_times), onset - _BASELINE_GAP)
    window_end = boundary
    pods = _bound_pods(actor, _containers(latest_after.body), history, window_start, window_end)
    if pods is None:
        return None
    if _oom_or_eviction(pods, findings, history):
        return None
    records = read_pressure(pods, window_start)
    coverage: list[PodCoverage] = []
    for pod in pods:
        pod_start = max(window_start, _created(history[pod]) or window_start)
        for container, resource in lowered:
            matching = [
                item
                for item in records
                if item.pod == pod and item.container == container and item.resource == resource
            ]
            if len(matching) != 1:
                return None
            item = matching[0]
            if (
                item.sample_count < 2
                or item.sample_start is None
                or item.sample_end is None
                or item.sample_start > pod_start + _STEP
                or item.sample_end < window_end - _STEP
                or item.peak >= PRESSURE_THRESHOLD[resource]
            ):
                return None
            coverage.append(
                PodCoverage(
                    pod=pod,
                    container=container,
                    resource=resource,
                    window_start=pod_start,
                    window_end=window_end,
                    peak=item.peak,
                    evidence_id=item.evidence_id,
                )
            )
    change_ids = tuple(dict.fromkeys(e for f in initiating for e in f.evidence_ids))
    lowered_text = ", ".join(f"{c}.{r}" for c, r in lowered)
    return MechanismMismatch(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=actor,
        lowered=tuple(lowered),
        pods=pods,
        onset=onset,
        boundary=boundary,
        window_start=window_start,
        window_end=window_end,
        coverage=tuple(coverage),
        change_evidence_ids=change_ids,
        preconditions=(
            EliminationPrecondition(
                name="resource_only_limit_reduction",
                passed=True,
                detail=f"initiating change only lowers/sets limits: {lowered_text}",
            ),
            EliminationPrecondition(
                name="bound_pods_positively_determined",
                passed=True,
                detail=f"{len(pods)} Pod(s) of the post-change template",
            ),
            EliminationPrecondition(
                name="observed_normal_full_coverage",
                passed=True,
                detail=(
                    f"{len(coverage)}/{len(pods) * len(lowered)} pod x resource series below "
                    "threshold across the window"
                ),
            ),
            EliminationPrecondition(
                name="no_positive_pressure_evidence",
                passed=True,
                detail="no RESOURCE_PRESSURE, OOMKilled or Evicted on the bound Pods",
            ),
        ),
    )


def assess_resource_mechanisms(
    hypotheses: Sequence[Hypothesis],
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    findings: Sequence[Finding],
    read_pressure: PressureReader,
    onset: datetime | None,
    grace: timedelta,
) -> dict[str, MechanismMismatch]:
    result: dict[str, MechanismMismatch] = {}
    for hypothesis in hypotheses:
        item = assess_resource_mechanism(
            hypothesis,
            history=history,
            findings=findings,
            read_pressure=read_pressure,
            onset=onset,
            grace=grace,
        )
        if item is not None:
            result[hypothesis.hypothesis_id] = item
    return result


__all__ = [
    "RULE_ID",
    "RULE_VERSION",
    "MechanismMismatch",
    "PodCoverage",
    "assess_resource_mechanism",
    "assess_resource_mechanisms",
]
