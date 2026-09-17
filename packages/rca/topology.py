"""Structural relations derived from observed Kubernetes objects and events."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from packages.rca.json_access import child, mapping
from packages.rca.model import CLUSTER_SCOPE, ClusterEvent, Edge, EntityRef, ObjectVersion

WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})
# Relations where one object is legitimately shared by many unrelated workloads
# (a NetworkPolicy selecting many pods, a ConfigMap used by many deployments).
# Crossing one of these to reach a third, unrelated object is not a causal
# claim: sharing a policy or a ConfigMap does not mean two workloads share fate.
FAN_OUT_RELATIONS = frozenset({"restricts", "uses_config"})
FAN_OUT_THRESHOLD = 3
_CHAOS_TARGET = re.compile(r"apply chaos for ([a-z0-9.-]+)/([a-z0-9.-]+)", re.IGNORECASE)
_NAME_LABELS = ("app.kubernetes.io/name", "app.kubernetes.io/component", "app", "k8s-app")


def is_chaos_kind(kind: str) -> bool:
    return kind.endswith("Chaos") or kind == "Schedule" or kind == "Workflow"


def _labels(body: Mapping[str, Any]) -> dict[str, str]:
    metadata = body.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def _selector_matches(selector: Any, labels: Mapping[str, str]) -> bool:
    if not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels", selector)
    if not isinstance(match_labels, dict):
        return False
    return all(labels.get(str(key)) == str(value) for key, value in match_labels.items())


def _config_refs(pod_spec: Mapping[str, Any]) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    volumes = pod_spec.get("volumes") or []
    for volume in volumes if isinstance(volumes, list) else []:
        if not isinstance(volume, dict):
            continue
        for key, kind, name_key in (
            ("configMap", "ConfigMap", "name"),
            ("secret", "Secret", "secretName"),
        ):
            value = volume.get(key)
            if isinstance(value, dict) and isinstance(value.get(name_key), str):
                refs.add((kind, value[name_key]))
        projected = volume.get("projected")
        sources = projected.get("sources") if isinstance(projected, dict) else None
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict):
                continue
            for key, kind in (("configMap", "ConfigMap"), ("secret", "Secret")):
                value = source.get(key)
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    refs.add((kind, value["name"]))
    containers = [
        *(pod_spec.get("containers") or []),
        *(pod_spec.get("initContainers") or []),
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for env_from in container.get("envFrom") or []:
            if not isinstance(env_from, dict):
                continue
            for key, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                value = env_from.get(key)
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    refs.add((kind, value["name"]))
        for env in container.get("env") or []:
            source = env.get("valueFrom") if isinstance(env, dict) else None
            if not isinstance(source, dict):
                continue
            for key, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
                value = source.get(key)
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    refs.add((kind, value["name"]))
    return refs


_ENV_REF = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
_HOST = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]+)?(?:/.*)?$"
)
_ENDPOINT_NAMES = ("_ADDR", "_HOST", "_URL", "_URI", "_ENDPOINT", "_SERVICE", "_ADDRESS")


def _declared_hosts(
    pod_spec: Mapping[str, Any], config_data: Mapping[str, Mapping[str, Any]] | None = None
) -> set[tuple[str, str]]:
    """Hosts referenced by container environment values, as (host, env name).

    ``config_data`` maps ConfigMap names to their data so ``envFrom`` values count too.
    """
    hosts: set[tuple[str, str]] = set()
    containers = pod_spec.get("containers") or []
    for container in containers if isinstance(containers, list) else []:
        if not isinstance(container, dict):
            continue
        values: dict[str, str] = {}
        for env_from in container.get("envFrom") or []:
            ref = mapping(mapping(env_from).get("configMapRef"))
            data = (config_data or {}).get(str(ref.get("name")), {})
            values.update({str(k): str(v) for k, v in data.items() if isinstance(v, str)})
        env = container.get("env")
        for item in env if isinstance(env, list) else []:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                values[str(item.get("name"))] = item["value"]

        def resolve(match: re.Match[str], known: dict[str, str] = values) -> str:
            return known.get(match.group(1), "")

        for name, raw in values.items():
            value = _ENV_REF.sub(resolve, raw).strip()
            if not value or not (name.endswith(_ENDPOINT_NAMES) or ":" in value):
                continue
            match = _HOST.match(value.lower())
            if match and match.group(1) not in {"localhost", "0.0.0.0", "127.0.0.1"}:
                hosts.add((match.group(1), name))
    return hosts


def _pod_spec(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    spec = body.get("spec")
    if not isinstance(spec, dict):
        return None
    if body.get("kind") == "Pod":
        return spec
    template = spec.get("template")
    if body.get("kind") == "CronJob":
        job = spec.get("jobTemplate")
        job_spec = job.get("spec") if isinstance(job, dict) else None
        template = job_spec.get("template") if isinstance(job_spec, dict) else None
    inner = template.get("spec") if isinstance(template, dict) else None
    return inner if isinstance(inner, dict) else None


def derive_edges(
    latest: Mapping[EntityRef, ObjectVersion], events: Sequence[ClusterEvent] = ()
) -> tuple[Edge, ...]:
    """Derive owner, selection, configuration, scaling, policy, and fault edges."""
    edges: set[Edge] = set()
    pods_by_namespace: dict[str, list[tuple[EntityRef, dict[str, str]]]] = {}
    services = {(ref.namespace, ref.name): ref for ref in latest if ref.kind == "Service"}
    config_data: dict[str, dict[str, dict[str, Any]]] = {}
    for ref, version in latest.items():
        if ref.kind == "ConfigMap":
            config_data.setdefault(ref.namespace, {})[ref.name] = child(version.body, "data")
    for ref, version in latest.items():
        if ref.kind == "Pod":
            pods_by_namespace.setdefault(ref.namespace, []).append((ref, _labels(version.body)))
    for ref, version in latest.items():
        body = version.body
        metadata = child(body, "metadata")
        for owner in metadata.get("ownerReferences") or []:
            if isinstance(owner, dict) and owner.get("kind") and owner.get("name"):
                target = EntityRef(
                    kind=str(owner["kind"]), name=str(owner["name"]), namespace=ref.namespace
                )
                edges.add(Edge(source=ref, target=target, relation="owned_by"))
        spec = child(body, "spec")
        if ref.kind == "Service" and isinstance(spec.get("selector"), dict) and spec["selector"]:
            for pod, labels in pods_by_namespace.get(ref.namespace, []):
                if _selector_matches(spec["selector"], labels):
                    edges.add(Edge(source=ref, target=pod, relation="selects"))
        if ref.kind == "HorizontalPodAutoscaler":
            scale_target = mapping(spec.get("scaleTargetRef"))
            if scale_target.get("kind") and scale_target.get("name"):
                edges.add(
                    Edge(
                        source=ref,
                        target=EntityRef(
                            kind=str(scale_target["kind"]),
                            name=str(scale_target["name"]),
                            namespace=ref.namespace,
                        ),
                        relation="scales",
                    )
                )
        if ref.kind == "NetworkPolicy":
            selector = spec.get("podSelector")
            for pod, labels in pods_by_namespace.get(ref.namespace, []):
                selects_all = isinstance(selector, dict) and not selector
                if selects_all or _selector_matches(selector, labels):
                    edges.add(Edge(source=ref, target=pod, relation="restricts"))
        if is_chaos_kind(ref.kind):
            selector = spec.get("selector")
            if isinstance(selector, dict):
                namespaces = selector.get("namespaces") or []
                label_selector = selector.get("labelSelectors") or {}
                for namespace in namespaces if isinstance(namespaces, list) else []:
                    for pod, labels in pods_by_namespace.get(str(namespace), []):
                        if _selector_matches(label_selector, labels):
                            edges.add(Edge(source=ref, target=pod, relation="disrupts"))
        pod_spec = _pod_spec(body)
        if pod_spec is not None and ref.kind != "Pod":
            for host, _env_name in _declared_hosts(pod_spec, config_data.get(ref.namespace)):
                parts = host.split(".")
                namespace = parts[1] if len(parts) > 1 and parts[1] != "svc" else ref.namespace
                target_service = services.get((namespace, parts[0]))
                if target_service is not None and target_service.name != ref.name:
                    edges.add(Edge(source=ref, target=target_service, relation="calls"))
        if pod_spec is not None:
            for kind, name in _config_refs(pod_spec):
                edges.add(
                    Edge(
                        source=ref,
                        target=EntityRef(kind=kind, name=name, namespace=ref.namespace),
                        relation="uses_config",
                    )
                )
    owners = {edge.source: edge.target for edge in edges if edge.relation == "owned_by"}
    for edge in [e for e in edges if e.relation == "selects"]:
        current = edge.target
        for _ in range(3):
            current = owners.get(current, current)
        if current.kind in WORKLOAD_KINDS:
            edges.add(Edge(source=edge.source, target=current, relation="routes_to"))
    schedules = {ref for ref in latest if ref.kind == "Schedule"}
    for event in events:
        if not is_chaos_kind(event.entity.kind):
            continue
        match = _CHAOS_TARGET.search(event.message)
        if match:
            pod = EntityRef(kind="Pod", name=match.group(2), namespace=match.group(1))
            edges.add(Edge(source=event.entity, target=pod, relation="disrupts"))
        if event.entity.kind == "Schedule":
            schedules.add(event.entity)
    chaos_instances = {event.entity for event in events if event.entity.kind.endswith("Chaos")}
    for schedule in schedules:
        for instance in chaos_instances:
            if (
                instance.namespace == schedule.namespace
                and instance.name.startswith(f"{schedule.name}-")
                and len(instance.name) - len(schedule.name) <= 7
            ):
                edges.add(Edge(source=schedule, target=instance, relation="spawns"))
    return tuple(sorted(edges, key=lambda e: (e.source.canonical, e.relation, e.target.canonical)))


class Topology:
    """Undirected lookups over derived edges plus workload/service naming."""

    def __init__(self, edges: Iterable[Edge], latest: Mapping[EntityRef, ObjectVersion]) -> None:
        self.edges = tuple(edges)
        self.latest = latest
        self._adjacent: dict[EntityRef, set[tuple[EntityRef, str]]] = {}
        self._out: dict[EntityRef, list[Edge]] = {}
        self._in: dict[EntityRef, list[Edge]] = {}
        self._names: dict[EntityRef, set[str]] = {}
        self._reach: dict[tuple[EntityRef, int], dict[EntityRef, int]] = {}
        self._causal_reach: dict[tuple[EntityRef, int], dict[EntityRef, int]] = {}
        self._relation_degree: dict[tuple[EntityRef, str], int] = {}
        for edge in self.edges:
            self._adjacent.setdefault(edge.source, set()).add((edge.target, edge.relation))
            self._adjacent.setdefault(edge.target, set()).add((edge.source, edge.relation))
            self._out.setdefault(edge.source, []).append(edge)
            self._in.setdefault(edge.target, []).append(edge)
            if edge.relation in FAN_OUT_RELATIONS:
                # Count degree on both ends: a NetworkPolicy is a hub by how
                # many pods it restricts (its outgoing side); a ConfigMap is a
                # hub by how many workloads use it (its incoming side).
                for node in (edge.source, edge.target):
                    key = (node, edge.relation)
                    self._relation_degree[key] = self._relation_degree.get(key, 0) + 1

    def neighbors(self, entity: EntityRef) -> tuple[tuple[EntityRef, str], ...]:
        return tuple(
            sorted(self._adjacent.get(entity, ()), key=lambda item: (item[0].canonical, item[1]))
        )

    def outgoing(self, entity: EntityRef, relation: str | None = None) -> tuple[EntityRef, ...]:
        return tuple(
            edge.target
            for edge in self._out.get(entity, ())
            if relation is None or edge.relation == relation
        )

    def incoming(self, entity: EntityRef, relation: str | None = None) -> tuple[EntityRef, ...]:
        return tuple(
            edge.source
            for edge in self._in.get(entity, ())
            if relation is None or edge.relation == relation
        )

    def workload_of(self, entity: EntityRef) -> EntityRef | None:
        """Follow ownership up to the controlling workload."""
        current = entity
        for _ in range(4):
            if current.kind in WORKLOAD_KINDS:
                return current
            owners = self.outgoing(current, "owned_by")
            if not owners:
                break
            current = owners[0]
        if current.kind in WORKLOAD_KINDS:
            return current
        if entity.kind == "Pod":
            guess = pod_workload_name(entity.name)
            for kind in ("Deployment", "StatefulSet", "DaemonSet"):
                ref = EntityRef(kind=kind, name=guess, namespace=entity.namespace)
                if ref in self.latest:
                    return ref
        return None

    def service_names(self, entity: EntityRef) -> set[str]:
        """Names under which telemetry and alerts may refer to this entity."""
        cached = self._names.get(entity)
        if cached is not None:
            return cached
        names = {entity.name}
        workload = self.workload_of(entity)
        if workload is not None:
            names.add(workload.name)
        if entity.kind == "Pod":
            names.add(pod_workload_name(entity.name))
        for ref in (entity, workload):
            version = self.latest.get(ref) if ref is not None else None
            if version is None:
                continue
            labels = _labels(version.body)
            names.update(labels[key] for key in _NAME_LABELS if key in labels)
        result = {name for name in names if name}
        self._names[entity] = result
        return result

    def entities_for_service(self, service: str, namespace: str | None) -> set[EntityRef]:
        """Workloads and services whose names match an alert's service label."""
        result: set[EntityRef] = set()
        for ref in self.latest:
            if namespace and ref.namespace not in {namespace, CLUSTER_SCOPE}:
                continue
            if ref.kind in WORKLOAD_KINDS or ref.kind in {"Service", "Pod"}:
                if service in self.service_names(ref):
                    result.add(ref)
        return result

    def reachable(self, start: EntityRef, *, max_depth: int = 4) -> dict[EntityRef, int]:
        """Undirected hop counts from ``start`` to every node within ``max_depth``."""
        key = (start, max_depth)
        cached = self._reach.get(key)
        if cached is not None:
            return cached
        depths = {start: 0}
        queue: deque[EntityRef] = deque([start])
        while queue:
            node = queue.popleft()
            depth = depths[node]
            if depth >= max_depth:
                continue
            for neighbor, _relation in self._adjacent.get(node, ()):
                if neighbor not in depths:
                    depths[neighbor] = depth + 1
                    queue.append(neighbor)
        self._reach[key] = depths
        return depths

    def distance(
        self, start: EntityRef, targets: set[EntityRef], *, max_depth: int = 4
    ) -> int | None:
        """Shortest undirected hop count from ``start`` to any target."""
        depths = self.reachable(start, max_depth=max_depth)
        found = [depths[target] for target in targets if target in depths]
        return min(found) if found else None

    def _is_fan_out_hub(self, node: EntityRef, relation: str) -> bool:
        return self._relation_degree.get((node, relation), 0) > FAN_OUT_THRESHOLD

    def causal_reachable(self, start: EntityRef, *, max_depth: int = 4) -> dict[EntityRef, int]:
        """Like ``reachable``, but does not cross a shared policy or config to a
        third object: two workloads that merely use the same ConfigMap, or sit
        behind the same NetworkPolicy along with many others, are not linked.
        """
        key = (start, max_depth)
        cached = self._causal_reach.get(key)
        if cached is not None:
            return cached
        depths = {start: 0}
        queue: deque[EntityRef] = deque([start])
        while queue:
            node = queue.popleft()
            depth = depths[node]
            if depth >= max_depth:
                continue
            for neighbor, relation in self._adjacent.get(node, ()):
                if neighbor in depths:
                    continue
                # A hub may explain its own direct targets (the start node's
                # primary claim); it may not hand off to a third, unrelated one.
                if (
                    relation in FAN_OUT_RELATIONS
                    and node != start
                    and self._is_fan_out_hub(node, relation)
                ):
                    continue
                depths[neighbor] = depth + 1
                queue.append(neighbor)
        self._causal_reach[key] = depths
        return depths

    def causal_distance(
        self, start: EntityRef, targets: set[EntityRef], *, max_depth: int = 4
    ) -> int | None:
        """Shortest hop count from ``start`` to any target, not through a shared hub."""
        depths = self.causal_reachable(start, max_depth=max_depth)
        found = [depths[target] for target in targets if target in depths]
        return min(found) if found else None


def pod_workload_name(pod_name: str) -> str:
    """Strip ReplicaSet/StatefulSet suffixes from a pod name."""
    parts = pod_name.split("-")
    if len(parts) >= 3 and len(parts[-1]) == 5 and len(parts[-2]) in (8, 9, 10):
        return "-".join(parts[:-2])
    if len(parts) >= 2 and len(parts[-1]) == 5:
        return "-".join(parts[:-1])
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1])
    return pod_name


__all__ = [
    "FAN_OUT_RELATIONS",
    "FAN_OUT_THRESHOLD",
    "WORKLOAD_KINDS",
    "Topology",
    "derive_edges",
    "is_chaos_kind",
    "pod_workload_name",
]
