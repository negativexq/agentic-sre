"""Deterministic signal extraction: symptoms, changes, fault injection, failures."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from packages.rca.json_access import child
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    Finding,
    FindingKind,
    LogRecord,
    ObjectVersion,
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
POLICY_KINDS = frozenset(
    {"NetworkPolicy", "ResourceQuota", "LimitRange", "HorizontalPodAutoscaler"}
)
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


def _diff_paths(before: Any, after: Any, path: str = "") -> list[str]:
    """Paths whose values differ; named list items (containers, env) are matched by name."""
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            paths.extend(_diff_paths(before.get(key), after.get(key), f"{path}.{key}"))
        return paths
    if isinstance(before, list) and isinstance(after, list) and before != after:
        old, new = _named(before), _named(after)
        if old is not None and new is not None:
            paths = []
            for name in sorted(set(old) | set(new)):
                paths.extend(_diff_paths(old.get(name), new.get(name), f"{path}[{name}]"))
            return paths
        if len(before) == len(after):
            paths = []
            for index, (left, right) in enumerate(zip(before, after, strict=True)):
                paths.extend(_diff_paths(left, right, f"{path}[{index}]"))
            return paths
    return [] if before == after else [path or "."]


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
            paths = _diff_paths(before, after)
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
                    summary = f"spec changed: {', '.join(meaningful[:3])}"
            else:
                kind = FindingKind.SPEC_CHANGE
                summary = f"{entity.kind} changed: {', '.join(paths[:3])}"
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


def _is_restrictive(entity: EntityRef, body: Mapping[str, Any]) -> bool:
    spec = child(body, "spec")
    if entity.kind == "NetworkPolicy":
        types = spec.get("policyTypes") or ["Ingress"]
        ingress = spec.get("ingress")
        egress = spec.get("egress")
        return ("Ingress" in types and not ingress) or ("Egress" in types and not egress)
    if entity.kind == "ResourceQuota":
        return True
    if entity.kind == "LimitRange":
        return True
    return False


def policy_findings(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    topology: Topology,
    namespaces: set[str],
) -> list[Finding]:
    """Restrictive policies in symptom namespaces, and fault objects present as objects."""
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
        if entity.kind not in POLICY_KINDS or entity.namespace not in namespaces:
            continue
        if entity.kind == "HorizontalPodAutoscaler" or not _is_restrictive(entity, latest.body):
            continue
        affected = topology.outgoing(entity, "restricts")
        findings.append(
            Finding(
                kind=FindingKind.POLICY_CREATED,
                entity=entity,
                at=_creation_time(latest.body),
                summary=f"restrictive {entity.kind} affecting {len(affected)} pod(s)",
                evidence_ids=(latest.evidence_id,),
                related=affected,
                details={"affected": [ref.canonical for ref in affected[:10]]},
            )
        )
    return findings


def _creation_time(body: Mapping[str, Any]) -> datetime | None:
    metadata = body.get("metadata")
    value = metadata.get("creationTimestamp") if isinstance(metadata, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fault_event_findings(events: Sequence[ClusterEvent], topology: Topology) -> list[Finding]:
    """Chaos experiments applied during the observed period, one per experiment object."""
    grouped: dict[EntityRef, list[ClusterEvent]] = {}
    for event in events:
        if is_chaos_kind(event.entity.kind):
            grouped.setdefault(event.entity, []).append(event)
    findings: list[Finding] = []
    for entity, items in grouped.items():
        reasons = Counter(item.reason for item in items)
        times = [t for item in items for t in (item.last_at, item.first_at) if t is not None]
        applied = [item for item in items if item.reason in {"Applied", "Started"}]
        if entity.kind == "Schedule":
            if not reasons.get("Spawned"):
                continue
            kind = FindingKind.FAULT_SCHEDULE
            summary = f"chaos schedule spawned {reasons['Spawned']} experiment(s)"
            targets = tuple(
                target
                for child in topology.outgoing(entity, "spawns")
                for target in topology.outgoing(child, "disrupts")
            )
        else:
            if not applied:
                continue
            kind = FindingKind.FAULT_INJECTION
            summary = f"{entity.kind} applied fault ({reasons.get('Applied', 0)} application(s))"
            targets = topology.outgoing(entity, "disrupts")
        findings.append(
            Finding(
                kind=kind,
                entity=entity,
                at=max(times) if times else None,
                summary=summary,
                evidence_ids=tuple(item.evidence_id for item in items[:6]),
                related=tuple(dict.fromkeys(targets)),
                details={
                    "first_at": min(times).isoformat() if times else None,
                    "last_at": max(times).isoformat() if times else None,
                    "reasons": dict(reasons),
                    "targets": sorted({t.canonical for t in targets}),
                    "failed_applications": reasons.get("Failed", 0),
                },
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
    "dependency_findings",
    "change_findings",
    "extract_symptoms",
    "failure_findings",
    "fault_event_findings",
    "is_background_alert",
    "policy_findings",
    "symptom_entities",
]
