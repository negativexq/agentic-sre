"""Deterministic signal extraction: symptoms, changes, fault injection, failures."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from packages.rca.json_access import child, mapping
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    Finding,
    FindingKind,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    Symptoms,
)
from packages.rca.topology import WORKLOAD_KINDS, Topology, is_chaos_kind

# Alerts that fire continuously on healthy clusters (compared case-insensitively).
BACKGROUND_ALERTS = frozenset(
    {
        "watchdog",
        "infoinhibitor",
        "prometheusnotconnectedtoalertmanagers",
        "kubeclientcertificateexpiration",
        "kubeschedulerdown",
        "kubecontrollermanagerdown",
    }
)
# Objects whose versions change as a side effect of normal operation.
DERIVED_KINDS = frozenset(
    {
        "Pod",
        "ReplicaSet",
        "ControllerRevision",
        "Endpoints",
        "EndpointSlice",
        "Event",
        "Lease",
        "Node",
        "PersistentVolume",
        "PersistentVolumeClaim",
        "VolumeAttachment",
        "CustomResourceDefinition",
        "APIService",
    }
)
ACCESS_KINDS = frozenset(
    {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
)
CONFIG_KINDS = frozenset({"ConfigMap", "Secret"})
_IGNORED_PATHS = ("metadata", "status")
_RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
_LABEL_KEYS = ("service_name", "service", "job_name")


def is_background_alert(name: str) -> bool:
    return name.casefold() in BACKGROUND_ALERTS


def extract_symptoms(alerts: Sequence[Alert]) -> Symptoms:
    """Summarize diagnostic alerts; platform-health alerts only count as background."""
    background: Counter[str] = Counter()
    diagnostic: list[Alert] = []
    for alert in alerts:
        if is_background_alert(alert.name):
            background[alert.name] += 1
        else:
            diagnostic.append(alert)
    services: set[str] = set()
    namespaces: set[str] = set()
    for alert in diagnostic:
        if alert.service:
            services.add(alert.service)
        for key in _LABEL_KEYS:
            if alert.labels.get(key):
                services.add(alert.labels[key])
        if alert.namespace:
            namespaces.add(alert.namespace)
    starts = [alert.starts_at for alert in diagnostic]
    return Symptoms(
        onset=min(starts) if starts else None,
        last_seen=max(starts) if starts else None,
        services=tuple(sorted(services)),
        namespaces=tuple(sorted(namespaces)),
        alert_names=tuple(sorted({alert.name for alert in diagnostic})),
        background_alert_counts=dict(sorted(background.items())),
    )


def symptom_entities(alerts: Sequence[Alert], topology: Topology) -> set[EntityRef]:
    """Objects the diagnostic alerts point at, by service, workload, or pod label."""
    result: set[EntityRef] = set()
    for alert in alerts:
        if is_background_alert(alert.name):
            continue
        namespace = alert.namespace or alert.labels.get("namespace")
        names = {alert.service} if alert.service else set()
        names.update(alert.labels[key] for key in _LABEL_KEYS if alert.labels.get(key))
        for name in names:
            if name:
                result.update(topology.entities_for_service(name, namespace))
        for key, kind in (
            ("pod", "Pod"),
            ("deployment", "Deployment"),
            ("statefulset", "StatefulSet"),
        ):
            value = alert.labels.get(key)
            if value and namespace:
                ref = EntityRef(kind=kind, name=value, namespace=namespace)
                if ref in topology.latest:
                    result.add(ref)
                    workload = topology.workload_of(ref)
                    if workload is not None:
                        result.add(workload)
    return result


def _content(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in body.items()
        if key not in _IGNORED_PATHS and key not in {"apiVersion", "kind"}
    }


def _named(items: list[Any]) -> dict[str, Any] | None:
    """Index a list of objects by their ``name`` field when every item has a unique one."""
    names = [
        str(item["name"])
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    if len(names) != len(items) or len(set(names)) != len(names):
        return None
    return dict(zip(names, items, strict=True))


def _diff(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Differing leaves as (path, old, new); named list items are matched by name."""
    if isinstance(before, dict) or isinstance(after, dict):
        if (before is None or isinstance(before, dict)) and (
            after is None or isinstance(after, dict)
        ):
            old_map, new_map = before or {}, after or {}
            changes: list[tuple[str, Any, Any]] = []
            for key in sorted(set(old_map) | set(new_map), key=str):
                changes.extend(_diff(old_map.get(key), new_map.get(key), f"{path}.{key}"))
            return changes
    if (isinstance(before, list) or before is None) and (isinstance(after, list) or after is None):
        old_list, new_list = before or [], after or []
        if old_list != new_list:
            old, new = _named(old_list), _named(new_list)
            if old is not None and new is not None:
                changes = []
                for name in sorted(set(old) | set(new)):
                    item_path = f"{path}[{name}]"
                    changes.extend(
                        change
                        for change in _diff(old.get(name), new.get(name), item_path)
                        if change[0] != f"{item_path}.name"
                    )
                return changes
            if len(old_list) == len(new_list):
                changes = []
                for index, (left, right) in enumerate(zip(old_list, new_list, strict=True)):
                    changes.extend(_diff(left, right, f"{path}[{index}]"))
                return changes
    return [] if before == after else [(path or ".", before, after)]


def _diff_paths(before: Any, after: Any, path: str = "") -> list[str]:
    return [item[0] for item in _diff(before, after, path)]


def _short(value: Any) -> str:
    if value is None:
        return "unset"
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 40 else text[:37] + "..."


def _describe_changes(changes: list[tuple[str, Any, Any]], limit: int = 3) -> str:
    """``env[X].value: unset -> 2500`` style text for the first few leaf changes."""
    parts = []
    for path, old, new in changes[:limit]:
        tail = path.replace(".spec.template.spec.containers", "").lstrip(".")
        parts.append(f"{tail}: {_short(old)} -> {_short(new)}")
    more = f" (+{len(changes) - limit} more)" if len(changes) > limit else ""
    return "; ".join(parts) + more


def _images(body: Mapping[str, Any]) -> list[str]:
    spec = body.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if not isinstance(containers, list):
        return []
    return [str(item.get("image")) for item in containers if isinstance(item, dict)]


def _config_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize which data keys changed and a short before/after excerpt."""
    changed: list[str] = []
    excerpts: dict[str, dict[str, str]] = {}
    for section in ("data", "stringData", "binaryData"):
        old = child(before, section)
        new = child(after, section)
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                changed.append(key)
                if section != "binaryData":
                    excerpts[key] = _changed_excerpt(str(old.get(key, "")), str(new.get(key, "")))
    return {"changed_keys": changed, "excerpts": excerpts}


def _changed_excerpt(old: str, new: str) -> dict[str, str]:
    """Show the region around the first difference, and parsed JSON differences if any."""
    try:
        old_json, new_json = json.loads(old), json.loads(new)
    except (ValueError, TypeError):
        old_json = new_json = None
    if old_json is not None and new_json is not None:
        paths = _diff_paths(old_json, new_json)
        return {"changed_paths": ", ".join(paths[:8])}
    index = next(
        (i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b),
        min(len(old), len(new)),
    )
    start = max(0, index - 40)
    return {"before": old[start : index + 80], "after": new[start : index + 80]}


def change_findings(history: Mapping[EntityRef, Sequence[ObjectVersion]]) -> list[Finding]:
    """Findings for every observed change between consecutive object versions."""
    findings: list[Finding] = []
    for entity, versions in history.items():
        if entity.kind in DERIVED_KINDS or entity.kind in ACCESS_KINDS or len(versions) < 2:
            continue
        for previous, current in zip(versions, versions[1:], strict=False):
            before, after = _content(previous.body), _content(current.body)
            leaves = _diff(before, after)
            paths = [leaf[0] for leaf in leaves]
            if not paths:
                continue
            details: dict[str, Any] = {
                "previous_observed_at": previous.observed_at.isoformat(),
                "changed_paths": paths[:12],
            }
            if entity.kind in CONFIG_KINDS:
                kind = FindingKind.CONFIG_CHANGE
                details.update(_config_diff(before, after))
                summary = f"{entity.kind} data changed: {', '.join(details['changed_keys'][:4])}"
            elif entity.kind in WORKLOAD_KINDS:
                meaningful = [p for p in paths if not p.endswith(_RESTART_ANNOTATION)]
                old_images, new_images = _images(previous.body), _images(current.body)
                if old_images != new_images:
                    kind = FindingKind.IMAGE_CHANGE
                    details.update({"before": old_images, "after": new_images})
                    summary = f"image changed to {', '.join(new_images)}"
                elif not meaningful:
                    kind = FindingKind.ROLLOUT_RESTART
                    summary = "rollout restart"
                elif all(p == ".spec.replicas" for p in meaningful):
                    kind = FindingKind.SCALE_CHANGE
                    spec_before = before.get("spec") or {}
                    spec_after = after.get("spec") or {}
                    details.update(
                        {
                            "before": spec_before.get("replicas"),
                            "after": spec_after.get("replicas"),
                        }
                    )
                    summary = f"replicas {details['before']} -> {details['after']}"
                else:
                    kind = FindingKind.SPEC_CHANGE
                    summary = "spec changed: " + _describe_changes(
                        [leaf for leaf in leaves if not leaf[0].endswith(_RESTART_ANNOTATION)]
                    )
            else:
                kind = FindingKind.SPEC_CHANGE
                summary = f"{entity.kind} changed: {_describe_changes(leaves)}"
            findings.append(
                Finding(
                    kind=kind,
                    entity=entity,
                    at=current.observed_at,
                    summary=summary,
                    evidence_ids=(previous.evidence_id, current.evidence_id),
                    details=details,
                )
            )
    return findings


_QUANTITY_SUFFIXES = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


def parse_quantity(value: Any) -> float | None:
    """Parse a Kubernetes resource quantity such as ``512Mi`` or ``250m``."""
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    for suffix in sorted(_QUANTITY_SUFFIXES, key=len, reverse=True):
        if value.endswith(suffix):
            number = value[: -len(suffix)]
            try:
                return float(number) * _QUANTITY_SUFFIXES[suffix]
            except ValueError:
                return None
    try:
        return float(value)
    except ValueError:
        return None


def _deny_all(spec: Mapping[str, Any]) -> list[str]:
    types = spec.get("policyTypes") or ["Ingress"]
    return [
        direction
        for direction in ("Ingress", "Egress")
        if direction in types and not spec.get(direction.lower())
    ]


def _allowed_ports(rules: Any) -> list[str]:
    ports: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        for port in mapping(rule).get("ports") or []:
            item = mapping(port)
            ports.append(f"{item.get('protocol', 'TCP')}/{item.get('port', '*')}")
    return sorted(set(ports))


def _network_policy_finding(
    entity: EntityRef, version: ObjectVersion, topology: Topology
) -> Finding | None:
    affected = topology.outgoing(entity, "restricts")
    if not affected:
        return None
    spec = child(version.body, "spec")
    denied = _deny_all(spec)
    details: dict[str, Any] = {"affected": [ref.canonical for ref in affected[:10]]}
    if denied:
        return Finding(
            kind=FindingKind.POLICY_CREATED,
            entity=entity,
            at=_creation_time(version.body),
            summary=f"NetworkPolicy denies all {'/'.join(denied).lower()} for {len(affected)} pod(s)",
            evidence_ids=(version.evidence_id,),
            related=affected,
            details={**details, "denied": denied},
        )
    ingress_ports = _allowed_ports(spec.get("ingress"))
    egress_ports = _allowed_ports(spec.get("egress"))
    limits = []
    if ingress_ports:
        limits.append(f"ingress only on {', '.join(ingress_ports)}")
    if egress_ports:
        limits.append(f"egress only on {', '.join(egress_ports)}")
    if not limits:
        limits.append("traffic only from listed peers")
    return Finding(
        kind=FindingKind.NETWORK_RESTRICTION,
        entity=entity,
        at=_creation_time(version.body),
        summary=f"NetworkPolicy allows {'; '.join(limits)} for {len(affected)} pod(s)",
        evidence_ids=(version.evidence_id,),
        related=affected,
        details={**details, "ingress_ports": ingress_ports, "egress_ports": egress_ports},
    )


def _quota_finding(
    entity: EntityRef,
    version: ObjectVersion,
    rejections: Sequence[ClusterEvent],
    topology: Topology,
) -> Finding | None:
    status = child(version.body, "status")
    hard, used = mapping(status.get("hard")), mapping(status.get("used"))
    exhausted = sorted(
        name
        for name, limit in hard.items()
        if (h := parse_quantity(limit)) is not None
        and (u := parse_quantity(used.get(name))) is not None
        and h > 0
        and u >= h
    )
    if rejections:
        rejected = tuple(dict.fromkeys(event.entity for event in rejections))
        workloads = tuple(dict.fromkeys(topology.workload_of(ref) or ref for ref in rejected))
        times = [t for e in rejections if (t := e.first_at or e.last_at) is not None]
        return Finding(
            kind=FindingKind.QUOTA_EXCEEDED,
            entity=entity,
            at=min(times) if times else None,
            summary=(
                f"{entity.kind} rejected pod creation {sum(e.count for e in rejections)} time(s) "
                f"for {', '.join(ref.name for ref in workloads[:3])}: {rejections[-1].message[:120]}"
            ),
            evidence_ids=(version.evidence_id, *(e.evidence_id for e in rejections[:3])),
            related=workloads,
            details={"exhausted": exhausted, "rejected": [ref.canonical for ref in rejected[:10]]},
        )
    if exhausted:
        return Finding(
            kind=FindingKind.QUOTA_EXHAUSTED,
            entity=entity,
            at=_creation_time(version.body),
            summary=f"{entity.kind} fully used for {', '.join(exhausted)}",
            evidence_ids=(version.evidence_id,),
            details={"exhausted": exhausted},
        )
    return None


_QUOTA_MESSAGE = re.compile(r"exceeded quota:?\s*([a-z0-9.-]+)?", re.IGNORECASE)
_LIMIT_MESSAGE = re.compile(r"forbidden: (maximum|minimum) .* per (container|pod)", re.IGNORECASE)


def policy_findings(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    topology: Topology,
    namespaces: set[str],
    events: Sequence[ClusterEvent] = (),
) -> list[Finding]:
    """Network restrictions, exhausted quotas, and fault objects present as objects."""
    quota_events: dict[str, list[ClusterEvent]] = {}
    limit_events: dict[str, list[ClusterEvent]] = {}
    for event in events:
        if match := _QUOTA_MESSAGE.search(event.message):
            key = f"{event.entity.namespace}/{match.group(1) or ''}"
            quota_events.setdefault(key, []).append(event)
        elif _LIMIT_MESSAGE.search(event.message):
            limit_events.setdefault(event.entity.namespace, []).append(event)
    findings: list[Finding] = []
    for entity, versions in history.items():
        latest = versions[-1]
        if is_chaos_kind(entity.kind):
            targets = topology.outgoing(entity, "disrupts")
            findings.append(
                Finding(
                    kind=(
                        FindingKind.FAULT_SCHEDULE
                        if entity.kind == "Schedule"
                        else FindingKind.FAULT_INJECTION
                    ),
                    entity=entity,
                    at=_creation_time(latest.body) or versions[0].observed_at,
                    summary=f"{entity.kind} object present",
                    evidence_ids=(latest.evidence_id,),
                    related=targets,
                    details={"targets": [t.canonical for t in targets]},
                )
            )
            continue
        finding: Finding | None = None
        if entity.kind == "NetworkPolicy" and entity.namespace in namespaces:
            finding = _network_policy_finding(entity, latest, topology)
        elif entity.kind == "ResourceQuota":
            named = quota_events.get(f"{entity.namespace}/{entity.name}", [])
            unnamed = quota_events.get(f"{entity.namespace}/", [])
            finding = _quota_finding(entity, latest, [*named, *unnamed], topology)
        elif entity.kind == "LimitRange" and limit_events.get(entity.namespace):
            finding = _quota_finding(entity, latest, limit_events[entity.namespace], topology)
        if finding is not None:
            findings.append(finding)
    return findings


_CONTAINER_WAITING = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
}
_CONTAINER_TERMINATED = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded"}


def container_findings(history: Mapping[EntityRef, Sequence[ObjectVersion]]) -> list[Finding]:
    """Container states that explain failures: OOM kills, crash loops, bad images or config."""
    findings: list[Finding] = []
    for entity, versions in history.items():
        if entity.kind != "Pod":
            continue
        latest = versions[-1]
        status = child(latest.body, "status")
        problems: list[dict[str, Any]] = []
        for item in [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]:
            container = mapping(item)
            waiting = mapping(mapping(container.get("state")).get("waiting"))
            last = mapping(mapping(container.get("lastState")).get("terminated"))
            restarts = int(container.get("restartCount") or 0)
            if waiting.get("reason") in _CONTAINER_WAITING:
                problems.append(
                    {
                        "container": container.get("name"),
                        "reason": waiting["reason"],
                        "message": str(waiting.get("message") or "")[:160],
                        "restarts": restarts,
                        "at": last.get("finishedAt") or status.get("startTime"),
                    }
                )
            elif restarts and last.get("reason") in _CONTAINER_TERMINATED:
                problems.append(
                    {
                        "container": container.get("name"),
                        "reason": last["reason"],
                        "message": f"exit code {last.get('exitCode')}",
                        "restarts": restarts,
                        "at": last.get("finishedAt"),
                    }
                )
        if not problems:
            continue
        first = problems[0]
        when = [t for p in problems if (t := _parse(p.get("at"))) is not None]
        findings.append(
            Finding(
                kind=FindingKind.CONTAINER_FAILURE,
                entity=entity,
                at=min(when) if when else None,
                summary=(
                    f"container {first['container']} {first['reason']}"
                    f" ({first['restarts']} restart(s)) {first['message']}".strip()
                ),
                evidence_ids=(latest.evidence_id,),
                details={"problems": problems[:5], "reason": first["reason"]},
            )
        )
    return findings


PRESSURE_THRESHOLD = {"memory": 0.9, "cpu": 0.25}
PRESSURE_BASELINE_FACTOR = 0.6


def resource_findings(pressures: Sequence[ResourcePressure]) -> list[Finding]:
    """Containers whose memory or CPU pressure appeared around the incident.

    Pressure that already existed before the incident is normal for that
    workload and is ignored, as is pressure without a baseline to compare with.
    """
    worst: dict[EntityRef, list[ResourcePressure]] = {}
    for item in pressures:
        threshold = PRESSURE_THRESHOLD.get(item.resource)
        if threshold is None or item.baseline is None or item.peak < threshold:
            continue
        if item.baseline <= item.peak * PRESSURE_BASELINE_FACTOR:
            worst.setdefault(item.pod, []).append(item)
    findings: list[Finding] = []
    for pod, items in worst.items():
        items.sort(key=lambda item: item.peak, reverse=True)
        parts = [
            f"{item.container} {item.resource} {_PRESSURE_NOUN[item.resource]} rose from "
            f"{item.baseline or 0:.0%} to {item.peak:.0%}"
            for item in items[:3]
        ]
        times = [item.at for item in items if item.at is not None]
        findings.append(
            Finding(
                kind=FindingKind.RESOURCE_PRESSURE,
                entity=pod,
                at=min(times) if times else None,
                summary="; ".join(parts),
                evidence_ids=tuple(item.evidence_id for item in items[:3]),
                details={
                    "resources": sorted({item.resource for item in items}),
                    "peak": round(items[0].peak, 3),
                },
            )
        )
    return findings


_PRESSURE_NOUN = {"memory": "use of limit", "cpu": "throttling"}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _creation_time(body: Mapping[str, Any]) -> datetime | None:
    metadata = body.get("metadata")
    value = metadata.get("creationTimestamp") if isinstance(metadata, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(items: Sequence[ClusterEvent]) -> tuple[datetime | None, datetime | None]:
    times = [t for item in items for t in (item.first_at, item.last_at) if t is not None]
    return (min(times), max(times)) if times else (None, None)


def _clock(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "?"


def fault_event_findings(events: Sequence[ClusterEvent], topology: Topology) -> list[Finding]:
    """Chaos experiments seen in events, one finding per experiment object.

    A finding's time is when the experiment started. Experiments spawned by a
    schedule also report how long the schedule has been injecting faults, because
    a recurring fault that began before the alerts can be the cause even when its
    latest run happened later.
    """
    grouped: dict[EntityRef, list[ClusterEvent]] = {}
    for event in events:
        if is_chaos_kind(event.entity.kind):
            grouped.setdefault(event.entity, []).append(event)
    parents = {edge.target: edge.source for edge in topology.edges if edge.relation == "spawns"}
    schedule_events: dict[EntityRef, list[ClusterEvent]] = {}
    for entity, items in grouped.items():
        owner = entity if entity.kind == "Schedule" else parents.get(entity)
        if owner is not None:
            schedule_events.setdefault(owner, []).extend(items)
    findings: list[Finding] = []
    for entity, items in grouped.items():
        reasons = Counter(item.reason for item in items)
        first, last = _span(items)
        parent = parents.get(entity)
        details: dict[str, Any] = {
            "first_at": first.isoformat() if first else None,
            "last_at": last.isoformat() if last else None,
            "reasons": dict(reasons),
            "failed_applications": reasons.get("Failed", 0),
        }
        if entity.kind == "Schedule":
            if not reasons.get("Spawned"):
                continue
            kind = FindingKind.FAULT_SCHEDULE
            active_from, active_to = _span(schedule_events.get(entity, items))
            summary = (
                f"chaos schedule injecting faults since {_clock(active_from)}, "
                f"last run {_clock(active_to)} ({reasons['Spawned']} experiment(s))"
            )
            first = active_from
            targets = tuple(
                target
                for child in topology.outgoing(entity, "spawns")
                for target in topology.outgoing(child, "disrupts")
            )
        else:
            # Started-but-failed experiments never injected a fault.
            if not reasons.get("Applied"):
                continue
            kind = FindingKind.FAULT_INJECTION
            targets = topology.outgoing(entity, "disrupts")
            summary = (
                f"{entity.kind} applied fault at {_clock(first)} "
                f"({reasons.get('Applied', 0)} application(s))"
            )
            if parent is not None:
                active_from, active_to = _span(schedule_events.get(parent, items))
                summary += (
                    f"; schedule {parent.name} has injected this fault repeatedly from "
                    f"{_clock(active_from)} to {_clock(active_to)}"
                )
                details["schedule"] = parent.canonical
                details["schedule_active_from"] = active_from.isoformat() if active_from else None
                details["schedule_active_to"] = active_to.isoformat() if active_to else None
        details["targets"] = sorted({t.canonical for t in targets})
        findings.append(
            Finding(
                kind=kind,
                entity=entity,
                at=first,
                summary=summary,
                evidence_ids=tuple(item.evidence_id for item in items[:6]),
                related=tuple(dict.fromkeys(targets)),
                details=details,
            )
        )
    return findings


def failure_findings(events: Sequence[ClusterEvent]) -> list[Finding]:
    """Warning events grouped per object and reason."""
    grouped: dict[tuple[EntityRef, str], list[ClusterEvent]] = {}
    for event in events:
        if event.type != "Warning" or is_chaos_kind(event.entity.kind):
            continue
        if event.entity.kind in {"PersistentVolume", "PersistentVolumeClaim", "VolumeAttachment"}:
            continue
        grouped.setdefault((event.entity, event.reason), []).append(event)
    findings: list[Finding] = []
    for (entity, reason), items in grouped.items():
        times = [t for item in items if (t := item.last_at or item.first_at) is not None]
        count = sum(item.count for item in items)
        findings.append(
            Finding(
                kind=FindingKind.FAILURE_EVENT,
                entity=entity,
                at=max(times) if times else None,
                summary=f"{reason} x{count}: {items[-1].message[:160]}",
                evidence_ids=tuple(item.evidence_id for item in items[:4]),
                details={"reason": reason, "count": count},
            )
        )
    return findings


_CONNECTION_ERROR = re.compile(
    r"connect|connection|refused|unreachable|unavailable|timed? ?out|timeout|no route|"
    r"dial tcp|name resolution|no such host|broken pipe|reset by peer",
    re.IGNORECASE,
)


def dependency_findings(
    logs: Sequence[LogRecord], topology: Topology, alerting: set[EntityRef]
) -> list[Finding]:
    """Declared dependencies of alerting callers that log connection errors."""
    errors: dict[str, list[LogRecord]] = {}
    for record in logs:
        if _CONNECTION_ERROR.search(record.message):
            errors.setdefault(record.service, []).append(record)
    fan_in: Counter[EntityRef] = Counter()
    callers: set[EntityRef] = set()
    for edge in topology.edges:
        if edge.relation == "calls":
            fan_in[edge.target] += 1
            callers.add(edge.source)
    shared = {service for service, count in fan_in.items() if count > max(2, len(callers) / 2)}
    findings: list[Finding] = []
    for caller in sorted(alerting, key=lambda ref: ref.canonical):
        if caller.kind not in WORKLOAD_KINDS:
            continue
        records = [r for name in topology.service_names(caller) for r in errors.get(name, [])]
        if not records:
            continue
        specific = [
            service
            for service in topology.outgoing(caller, "calls")
            # Shared infrastructure (telemetry, gateways) is not a specific suspect.
            if service not in alerting and service not in shared
        ]
        for service in specific:
            if len(specific) > 1:
                # With several dependencies, only blame the ones the errors name.
                token = service.name.casefold()
                named = [r for r in records if token in r.message.casefold()]
                if not named:
                    continue
            else:
                named = records
            pods = topology.outgoing(service, "selects")
            targets = pods or (service,)
            times = [r.at for r in named if r.at is not None]
            for target in targets:
                findings.append(
                    Finding(
                        kind=FindingKind.DEPENDENCY_ERRORS,
                        entity=target,
                        at=max(times) if times else None,
                        summary=(
                            f"{caller.name} logged {len(named)} connection error(s) and depends "
                            f"on {service.name}: {named[-1].message[:120]}"
                        ),
                        evidence_ids=tuple(r.evidence_id for r in named[:4]),
                        related=(service,),
                        details={
                            "caller": caller.canonical,
                            "service": service.canonical,
                            "errors": len(named),
                            "dependencies_of_caller": len(topology.outgoing(caller, "calls")),
                        },
                    )
                )
    return findings


__all__ = [
    "BACKGROUND_ALERTS",
    "resource_findings",
    "container_findings",
    "parse_quantity",
    "dependency_findings",
    "change_findings",
    "extract_symptoms",
    "failure_findings",
    "fault_event_findings",
    "is_background_alert",
    "policy_findings",
    "symptom_entities",
]
