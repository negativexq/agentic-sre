"""Bounded semantic SRE operations over the typed snapshot backend."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from packages.contracts import Incident
from packages.evals.itbench.contracts import ITBenchEvidenceCategory, parse_canonical_entity
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_packing import bounded_pack
from packages.evals.itbench.external_context import normalize_alerts
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

E9_SEMANTIC_OPERATIONS = (
    "INCIDENT_OVERVIEW",
    "ALERT_ANALYSIS",
    "TOPOLOGY_ANALYSIS",
    "RECENT_CHANGE_ANALYSIS",
    "ENTITY_CONTEXT",
    "EVENT_ANALYSIS",
    "LOG_ANALYSIS",
    "METRIC_ANOMALIES",
    "TRACE_ERROR_TREE",
    "SPEC_ANALYSIS",
    "COMPARE_REPLICAS",
    "VERIFY_TEMPORAL_ALIGNMENT",
)


@dataclass(frozen=True, slots=True)
class SemanticOperationSpec:
    """Declarative scope metadata for the model-visible operation registry."""

    name: str
    scope: Literal["GLOBAL", "TARGET"]
    requires_target: bool


_TARGET_OPERATIONS = frozenset(
    {
        "ENTITY_CONTEXT",
        "EVENT_ANALYSIS",
        "LOG_ANALYSIS",
        "METRIC_ANOMALIES",
        "TRACE_ERROR_TREE",
        "SPEC_ANALYSIS",
        "COMPARE_REPLICAS",
        "VERIFY_TEMPORAL_ALIGNMENT",
    }
)
E9_SEMANTIC_OPERATION_SPECS = tuple(
    SemanticOperationSpec(
        name, "TARGET" if name in _TARGET_OPERATIONS else "GLOBAL", name in _TARGET_OPERATIONS
    )
    for name in E9_SEMANTIC_OPERATIONS
)


class SemanticCapabilityResolver:
    """Resolve useful observable operations without model or ground truth access."""

    def __init__(
        self,
        backend: ITBenchSnapshotBackend,
        memory: E9CaseMemory,
        incident: Incident | None = None,
    ) -> None:
        self.backend, self.memory, self.incident = backend, memory, incident

    def resolve(self, target_handle: str | None = None) -> dict[str, dict[str, Any]]:
        cache: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = (
            self.backend._semantic_capability_cache
        )
        cache_key = (target_handle, self.incident.created_at.isoformat() if self.incident else None)
        if cache_key in cache:
            return cache[cache_key]
        canonical = self.memory.resolve(target_handle) if target_handle else None
        operations = E9SemanticOperations(
            self.backend, self.memory, self.incident, enable_discovery=False
        )
        result: dict[str, dict[str, Any]] = {}
        for spec in E9_SEMANTIC_OPERATION_SPECS:
            if spec.requires_target and canonical is None:
                result[spec.name] = {"available": False, "reason": "observable target required"}
                continue
            try:
                result[spec.name] = self._check(operations, spec.name, canonical)
            except (KeyError, TypeError, ValueError) as error:
                result[spec.name] = {
                    "available": False,
                    "reason": f"capability check failed: {error}"[:200],
                }
        cache[cache_key] = result
        return result

    def available_operations(
        self, *, phase: str, target_handle: str | None = None
    ) -> tuple[str, ...]:
        phase_operations = {
            "OBSERVE": (),
            "VERIFY": (
                "ENTITY_CONTEXT",
                "EVENT_ANALYSIS",
                "LOG_ANALYSIS",
                "METRIC_ANOMALIES",
                "TRACE_ERROR_TREE",
                "SPEC_ANALYSIS",
                "COMPARE_REPLICAS",
                "VERIFY_TEMPORAL_ALIGNMENT",
            ),
        }.get(phase, E9_SEMANTIC_OPERATIONS)
        availability = self.resolve(target_handle)
        completed = {
            (item.get("entity_handle"), item.get("operation"))
            for item in self.memory.state.get("operations_already_run", [])
        }
        scope_by_name = {spec.name: spec.scope for spec in E9_SEMANTIC_OPERATION_SPECS}
        return tuple(
            operation
            for operation in phase_operations
            if availability.get(operation, {}).get("available")
            and (
                (target_handle, operation)
                if scope_by_name[operation] == "TARGET"
                else (None, operation)
            )
            not in completed
        )

    @staticmethod
    def _check(
        operations: E9SemanticOperations, operation: str, canonical: str | None
    ) -> dict[str, Any]:
        if operation in {
            "INCIDENT_OVERVIEW",
            "ALERT_ANALYSIS",
            "TOPOLOGY_ANALYSIS",
        }:
            return {"available": True, "reason": None}
        if operation == "EVENT_ANALYSIS":
            value = (
                operations.backend.query(
                    ITBenchEvidenceCategory.K8S_EVENTS,
                    {"entity": canonical, "limit": operations._limit(1)},
                )
                if canonical
                else {}
            )
            available = bool(value.get("matching_count"))
            return {
                "available": available,
                "reason": None if available else "no matching event evidence",
            }
        if operation == "LOG_ANALYSIS":
            value = operations._log_analysis(canonical)
            available = bool(value.get("data_available") and value.get("patterns"))
            return {
                "available": available,
                "reason": None if available else "no structured log error evidence",
            }
        if operation in {"ENTITY_CONTEXT", "SPEC_ANALYSIS"}:
            return {"available": canonical is not None, "reason": None}
        if operation == "RECENT_CHANGE_ANALYSIS":
            value = operations._recent_changes(canonical)
            available = bool(value.get("change_history_available") and value.get("changes"))
            return {
                "available": available,
                "reason": None if available else "change history unavailable in immutable snapshot",
            }
        if canonical is None:
            return {"available": False, "reason": "observable target required"}
        if operation == "TRACE_ERROR_TREE":
            value = operations._trace_error_tree(canonical)
            available = bool(value.get("error_tree_available") and value.get("edges"))
            return {
                "available": available,
                "reason": None if available else "no usable trace service-edge/status fields",
            }
        if operation == "METRIC_ANOMALIES":
            value = operations._metric_anomalies(canonical)
            groups = value.get("aggregates_by_metric", {})
            useful = bool(
                isinstance(groups, dict)
                and any(
                    isinstance(item, dict)
                    and (item.get("delta") is not None or item.get("relative_change") is not None)
                    for item in groups.values()
                )
            )
            return {
                "available": useful,
                "reason": None if useful else "no candidate-relevant metric signal",
            }
        if operation == "COMPARE_REPLICAS":
            value = operations._compare_replicas(canonical)
            available = bool(value.get("comparison_available"))
            return {
                "available": available,
                "reason": None
                if available
                else "fewer than two comparable peer identities are observable",
            }
        if operation == "VERIFY_TEMPORAL_ALIGNMENT":
            value = operations._temporal_alignment(canonical)
            available = (
                bool(value.get("incident_start"))
                and bool(value.get("candidate_event_times"))
                and value.get("temporal_relation") not in {None, "UNKNOWN"}
            )
            return {
                "available": available,
                "reason": None
                if available
                else "incident/candidate timestamps or event semantics unavailable",
            }
        return {"available": False, "reason": "operation has no availability rule"}


class E9SemanticOperations:
    """Answer diagnostic questions and register evidence/provenance in memory."""

    def __init__(
        self,
        backend: ITBenchSnapshotBackend,
        memory: E9CaseMemory,
        incident: Incident | None = None,
        *,
        enable_discovery: bool = True,
        semantic_facade: bool = True,
    ) -> None:
        self.backend, self.memory, self.incident = backend, memory, incident
        self.enable_discovery = enable_discovery
        self.semantic_facade = semantic_facade

    def execute(self, operation: str, target_handle: str | None, turn: int) -> dict[str, Any]:
        if operation not in E9_SEMANTIC_OPERATIONS:
            raise ValueError(f"unsupported semantic operation: {operation}")
        canonical = self.memory.resolve(target_handle) if target_handle else None
        if target_handle and canonical is None:
            raise ValueError(f"unknown candidate handle: {target_handle}")
        self.memory.append(
            "OPERATION_REQUESTED", turn, {"entity_handle": target_handle, "operation": operation}
        )
        if operation == "INCIDENT_OVERVIEW":
            data, category = (
                {
                    "alerts": _alert_digest(normalize_alerts(self.backend)),
                    "candidates": list(self.memory.state["discovered_entities"].values())[:10],
                    "topology": list(self.backend.topology(limit=16)),
                },
                "incident",
            )
        elif operation == "ALERT_ANALYSIS":
            data, category = (
                self.backend.query(ITBenchEvidenceCategory.ALERTS, {"limit": self._limit(20)}),
                "alerts",
            )
        elif operation == "TOPOLOGY_ANALYSIS":
            data, category = (
                {
                    "edges": list(self.backend.topology(entity=canonical, limit=self._limit(20)))
                    if canonical
                    else list(self.backend.topology(limit=self._limit(20)))
                },
                "topology",
            )
        elif operation == "RECENT_CHANGE_ANALYSIS":
            data, category = self._recent_changes(canonical), "changes"
        elif operation == "ENTITY_CONTEXT":
            if canonical is None:
                raise ValueError("entity context requires a candidate handle")
            data, category = (
                self.backend.query_entity_context(canonical, 8, include_telemetry=False),
                "entity_context",
            )
        elif operation == "SPEC_ANALYSIS":
            if canonical is None:
                raise ValueError("spec analysis requires a candidate handle")
            data, category = self._spec_analysis(canonical), "spec"
        elif operation == "COMPARE_REPLICAS":
            if canonical is None:
                raise ValueError("replica comparison requires a candidate handle")
            data, category = self._compare_replicas(canonical), "replicas"
        elif operation == "EVENT_ANALYSIS":
            data, category = (
                self.backend.query(
                    ITBenchEvidenceCategory.K8S_EVENTS,
                    {"entity": canonical, "limit": self._limit(20)},
                ),
                "events",
            )
        elif operation == "LOG_ANALYSIS":
            if canonical is None:
                raise ValueError("log analysis requires a candidate handle")
            data, category = self._log_analysis(canonical), "logs"
        elif operation == "METRIC_ANOMALIES":
            data, category = self._metric_anomalies(canonical), "metrics"
        elif operation == "TRACE_ERROR_TREE":
            data, category = self._trace_error_tree(canonical), "traces"
        else:
            if canonical is None:
                raise ValueError("temporal verification requires a candidate handle")
            data, category = self._temporal_alignment(canonical), "temporal"
        summary = _bounded(data)
        if not self.semantic_facade:
            summary = {"legacy_result": summary}
        if self.enable_discovery:
            self._discover_related(summary, turn)
        evidence_handle = self.memory.add_evidence(
            turn=turn, handle=target_handle, operation=operation, category=category, summary=summary
        )
        self.memory.append(
            "OBSERVATION_COMPLETED",
            turn,
            {
                "entity_handle": target_handle,
                "operation": operation,
                "evidence_handle": evidence_handle,
                "category": category,
            },
        )
        return {
            "operation": operation,
            "entity_handle": target_handle,
            "category": category,
            "evidence_ref": evidence_handle,
            "summary": summary,
        }

    def _recent_changes(self, canonical: str | None) -> dict[str, Any]:
        objects = self.backend.query(
            ITBenchEvidenceCategory.K8S_OBJECTS,
            {"entity": canonical, "limit": self._limit(20)}
            if canonical
            else {"limit": self._limit(20)},
        )
        changes = []
        for item in objects.get("records", []):
            record = item.get("record", {})
            body = _json_body(record.get("Body")) if isinstance(record, dict) else None
            if body:
                metadata = (
                    body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
                )
                changes.append(
                    {
                        "entity": _canonical_from_body(body),
                        "kind": body.get("kind"),
                        "change_history_unavailable": True,
                        "observable_fields": sorted(
                            set([*(_keys(body.get("spec"))), *(_keys(metadata.get("annotations")))])
                        )[:20],
                        "source_evidence": item.get("evidence_id"),
                    }
                )
        return {
            "change_history_available": False,
            "changes": changes,
            "source_count": objects.get("matching_count", 0),
        }

    def _spec_analysis(self, canonical: str) -> dict[str, Any]:
        context = self.backend.query_entity_context(canonical, 8, include_telemetry=False)
        specs = []
        for item in context.get("object_records", []):
            record = item.get("record", {})
            body = _json_body(record.get("Body")) if isinstance(record, dict) else None
            if body:
                spec = body.get("spec", {}) if isinstance(body.get("spec"), dict) else {}
                workload_spec = (
                    spec.get("template", {}).get("spec", {})
                    if isinstance(spec.get("template"), dict)
                    and isinstance(spec.get("template", {}).get("spec"), dict)
                    else spec
                )
                specs.append(
                    {
                        "evidence_id": item.get("evidence_id"),
                        "kind": body.get("kind"),
                        "image": _images(spec),
                        "env_references": _env_refs(spec),
                        "resources": _resources(spec, workload_spec),
                        "replicas": spec.get("replicas"),
                        "selector": spec.get("selector"),
                        "config_references": _config_refs(spec),
                        "service_account": workload_spec.get("serviceAccountName"),
                        "scheduling": {
                            key: spec.get(key)
                            for key in ("nodeName", "nodeSelector", "affinity", "tolerations")
                            if workload_spec.get(key) is not None
                        },
                    }
                )
        return {
            "entity": canonical,
            "specs": specs,
            "configuration_dependencies": context.get("configuration_dependencies", []),
            "owner": context.get("ownership", []),
        }

    def _metric_anomalies(self, canonical: str | None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"limit": self._limit(12)}
        if canonical:
            parsed = parse_canonical_entity(canonical)
            key = {
                "service": "service",
                "pod": "pod",
                "deployment": "workload",
                "statefulset": "workload",
                "daemonset": "workload",
                "replicaset": "workload",
                "job": "workload",
                "node": "node",
            }.get(parsed.kind.casefold())
            if key:
                arguments[key] = parsed.name
            arguments["entity"] = canonical
        return self.backend.metric_analysis(arguments)

    def _log_analysis(self, canonical: str | None) -> dict[str, Any]:
        return self.backend.log_analysis(
            {"entity": canonical, "limit": self._limit(12)}
            if canonical
            else {"limit": self._limit(12)}
        )

    def _compare_replicas(self, canonical: str) -> dict[str, Any]:
        parsed_target = parse_canonical_entity(canonical)
        objects: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for item in self.backend.complete_source_records(ITBenchEvidenceCategory.K8S_OBJECTS):
            record = item.get("record", {})
            body = _json_body(record.get("Body")) if isinstance(record, dict) else None
            if not body or not isinstance(body.get("metadata"), dict):
                continue
            metadata = body["metadata"]
            kind, name = body.get("kind"), metadata.get("name")
            if not isinstance(kind, str) or not isinstance(name, str):
                continue
            namespace = metadata.get("namespace", "_cluster")
            objects.append((f"{namespace}/{kind}/{name}", body, metadata))
        target = next((item for item in objects if item[0] == canonical), None)
        if target is None:
            return {
                "comparison_available": False,
                "peer_count": 0,
                "spec_differences": [],
                "event_differences": [],
                "reason": "target object is not observable",
            }
        _target_canonical, target_body, target_metadata = target
        target_kind = str(target_body.get("kind", ""))
        target_owners = {
            (str(owner.get("kind")), str(owner.get("name")))
            for owner in target_metadata.get("ownerReferences", [])
            if isinstance(owner, dict)
            and isinstance(owner.get("kind"), str)
            and isinstance(owner.get("name"), str)
        }
        target_labels = target_metadata.get("labels", {})
        target_labels = target_labels if isinstance(target_labels, dict) else {}
        peers: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for candidate in objects:
            if (
                candidate[0] == canonical
                or candidate[2].get("namespace", "_cluster") != parsed_target.namespace
            ):
                continue
            candidate_body, candidate_metadata = candidate[1], candidate[2]
            candidate_kind = str(candidate_body.get("kind", ""))
            if target_kind == "Pod":
                if candidate_kind != "Pod":
                    continue
                owners = {
                    (str(owner.get("kind")), str(owner.get("name")))
                    for owner in candidate_metadata.get("ownerReferences", [])
                    if isinstance(owner, dict)
                    and isinstance(owner.get("kind"), str)
                    and isinstance(owner.get("name"), str)
                }
                same_owner = bool(target_owners & owners)
                same_labels = (
                    bool(target_labels)
                    and all(
                        candidate_metadata.get("labels", {}).get(key) == value
                        for key, value in target_labels.items()
                    )
                    if isinstance(candidate_metadata.get("labels"), dict)
                    else False
                )
                if same_owner or same_labels:
                    peers.append(candidate)
            elif candidate_kind == target_kind and target_owners:
                owners = {
                    (str(owner.get("kind")), str(owner.get("name")))
                    for owner in candidate_metadata.get("ownerReferences", [])
                    if isinstance(owner, dict)
                    and isinstance(owner.get("kind"), str)
                    and isinstance(owner.get("name"), str)
                }
                if target_owners & owners:
                    peers.append(candidate)
        peer_summaries = [_comparison_snapshot(item[1]) for item in peers]
        target_summary = _comparison_snapshot(target_body)
        spec_differences = _dict_differences(target_summary, peer_summaries)
        event_differences = []
        for peer in peers:
            peer_events = self.backend.query(
                ITBenchEvidenceCategory.K8S_EVENTS,
                {"entity": peer[0], "limit": self._limit(8)},
            )
            target_events = self.backend.query(
                ITBenchEvidenceCategory.K8S_EVENTS,
                {"entity": canonical, "limit": self._limit(8)},
            )
            if peer_events.get("matching_count") != target_events.get("matching_count"):
                event_differences.append(
                    {
                        "peer": peer[0],
                        "target_event_count": target_events.get("matching_count", 0),
                        "peer_event_count": peer_events.get("matching_count", 0),
                    }
                )
        return {
            "comparison_available": bool(peers),
            "peer_count": len(peers),
            "peers": [peer[0] for peer in peers],
            "spec_differences": spec_differences,
            "event_differences": event_differences,
            "reason": None
            if peers
            else "no owner/selector-related sibling identities are observable",
        }

    def _trace_error_tree(self, canonical: str | None) -> dict[str, Any]:
        args = (
            {"service": canonical.rsplit("/", 1)[-1], "limit": self._limit(40)}
            if canonical
            else {"limit": self._limit(40)}
        )
        result = self.backend.query(ITBenchEvidenceCategory.TRACES, args)
        edges: dict[tuple[str, str, str], int] = {}
        for item in result.get("records", []):
            record = item.get("record", {})
            if isinstance(record, dict):
                key = (
                    str(record.get("ServiceName", record.get("service", "unknown"))),
                    str(record.get("RemoteService", record.get("destination_service", "unknown"))),
                    str(
                        record.get(
                            "Status", record.get("status", record.get("StatusCode", "unknown"))
                        )
                    ),
                )
                if key[0] == "unknown" and key[1] == "unknown":
                    continue
                edges[key] = edges.get(key, 0) + 1
        return {
            "error_tree_available": bool(edges),
            "edges": [
                {"source_service": a, "destination_service": b, "status": c, "count": n}
                for (a, b, c), n in sorted(edges.items(), key=lambda pair: (-pair[1], pair[0]))[:12]
            ],
            "source_count": result.get("matching_count", 0),
        }

    def _temporal_alignment(self, canonical: str) -> dict[str, Any]:
        events = self.backend.query(
            ITBenchEvidenceCategory.K8S_EVENTS, {"entity": canonical, "limit": self._limit(20)}
        )
        times: list[dict[str, Any]] = []
        for item in events.get("records", []):
            body = (
                _json_body(item.get("record", {}).get("Body"))
                if isinstance(item.get("record"), dict)
                else None
            )
            value = (
                (body or {}).get("lastTimestamp", (body or {}).get("eventTime")) if body else None
            )
            parsed = _parse_time(value)
            if parsed:
                times.append({"time": parsed, "kind": _event_kind(body or {})})
        incident_time = self.incident.created_at if self.incident else None
        if incident_time is None or not times:
            delta, relation, support = None, "UNKNOWN", "INCONCLUSIVE"
        else:
            nearest = min(
                times, key=lambda value: abs((value["time"] - incident_time).total_seconds())
            )
            delta = (nearest["time"] - incident_time).total_seconds()
            relation, support = _temporal_assessment(delta, nearest["kind"])
        return {
            "entity": canonical,
            "incident_start": incident_time.isoformat() if incident_time else None,
            "candidate_event_times": [value["time"].isoformat() for value in times[:12]],
            "delta_seconds": delta,
            "temporal_relation": relation,
            "causal_temporal_assessment": support,
            "temporal_support": support,
        }

    def _discover_related(self, value: Any, turn: int = 0) -> None:
        found = []

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if (
                        key in {"entity", "canonical", "source", "target"}
                        and isinstance(child, str)
                        and child.count("/") == 2
                    ):
                        found.append({"canonical": child})
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
        if found:
            self.memory.discover_entities(tuple(found[:5]), turn=turn)

    def _limit(self, desired: int) -> int:
        return max(1, min(desired, self.backend.max_rows))


def _bounded(value: Any, limit: int = 5_500) -> Any:
    return bounded_pack(value, limit)


def _event_kind(body: dict[str, Any]) -> str:
    text = " ".join(str(body.get(key, "")) for key in ("reason", "type", "message")).casefold()
    if any(token in text for token in ("config", "change", "update", "patch", "rollout")):
        return "change"
    if any(token in text for token in ("fail", "error", "crash", "restart", "backoff")):
        return "failure"
    return "unknown"


def _temporal_assessment(delta: float, event_kind: str) -> tuple[str, str]:
    """Conservative temporal interpretation; sign alone is not causality."""
    if -300 <= delta < 0:
        return "NEAR_ONSET", "SUPPORTS"
    if 0 <= delta <= 300:
        return ("NEAR_ONSET" if delta <= 120 else "DURING"), "SUPPORTS"
    if delta < -300:
        return "BEFORE", "SUPPORTS" if event_kind == "change" else "INCONCLUSIVE"
    return "AFTER", "INCONCLUSIVE"


def _resources(spec: dict[str, Any], workload_spec: dict[str, Any]) -> Any:
    direct = workload_spec.get("resources", spec.get("resources"))
    result: dict[str, Any] = dict(direct) if isinstance(direct, dict) else {}
    containers: list[dict[str, Any]] = []
    for key, container_type in (("containers", "container"), ("initContainers", "initContainer")):
        values = workload_spec.get(key, [])
        if not isinstance(values, list):
            continue
        for index, container in enumerate(values):
            if not isinstance(container, dict) or not isinstance(container.get("resources"), dict):
                continue
            containers.append(
                {
                    "name": container.get("name", f"{container_type}-{index}"),
                    "type": container_type,
                    "resources": container["resources"],
                }
            )
    if containers:
        result["container_resources"] = containers
        # A single container's requests/limits are safe to expose at the
        # aggregate level.  For multiple containers retain per-container
        # values instead of pretending they are one scalar resource.
        for bound in ("requests", "limits"):
            if bound not in result:
                matching = [
                    entry["resources"][bound]
                    for entry in containers
                    if isinstance(entry["resources"].get(bound), dict)
                ]
                if len(matching) == 1:
                    result[bound] = matching[0]
    return result or None


def _comparison_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    metadata = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
    spec = body.get("spec", {}) if isinstance(body.get("spec"), dict) else {}
    status = body.get("status", {}) if isinstance(body.get("status"), dict) else {}
    return {
        "kind": body.get("kind"),
        "labels": metadata.get("labels", {}),
        "spec": {
            key: spec.get(key)
            for key in ("replicas", "selector", "nodeName", "containers", "initContainers")
            if spec.get(key) is not None
        },
        "status": {
            key: status.get(key)
            for key in ("phase", "readyReplicas", "availableReplicas", "restartCount")
            if status.get(key) is not None
        },
    }


def _dict_differences(target: dict[str, Any], peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for index, peer in enumerate(peers):
        for section in ("spec", "status", "labels"):
            target_section = target.get(section, {})
            peer_section = peer.get(section, {})
            if target_section != peer_section:
                differences.append(
                    {
                        "peer_index": index,
                        "section": section,
                        "target": target_section,
                        "peer": peer_section,
                    }
                )
    return differences


def _json_body(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _canonical_from_body(body: dict[str, Any]) -> str | None:
    metadata = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
    kind, name = body.get("kind"), metadata.get("name")
    return (
        f"{metadata.get('namespace', '_cluster')}/{kind}/{name}"
        if isinstance(kind, str) and isinstance(name, str)
        else None
    )


def _keys(value: Any) -> tuple[str, ...]:
    return tuple(value) if isinstance(value, dict) else ()


def _images(spec: dict[str, Any]) -> list[str]:
    template = (
        spec.get("template", {}).get("spec", {}) if isinstance(spec.get("template"), dict) else {}
    )
    return [
        str(item.get("image"))
        for item in template.get("containers", [])
        if isinstance(item, dict) and item.get("image")
    ]


def _env_refs(spec: dict[str, Any]) -> list[str]:
    template = (
        spec.get("template", {}).get("spec", {}) if isinstance(spec.get("template"), dict) else {}
    )
    return [
        str(item.get("name"))
        for c in template.get("containers", [])
        if isinstance(c, dict)
        for item in c.get("envFrom", [])
        if isinstance(item, dict) and item.get("name")
    ]


def _config_refs(value: Any) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"(?:configMap|secretName|name)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_.-]+)",
                json.dumps(value, default=str),
                flags=re.I,
            )
        )
    )[:20]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _alert_digest(alerts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    high = []
    for item in alerts:
        name = str(item.get("alertname", "unknown"))
        counts[name] = counts.get(name, 0) + int(item.get("occurrence_count", 1) or 1)
        if name.casefold() not in {"watchdog", "infoinhibitor"}:
            high.append(
                {
                    "alertname": name,
                    "labels": item.get("labels", {}),
                    "occurrence_count": item.get("occurrence_count", 1),
                }
            )
    return {
        "high_signal_alerts": high[:12],
        "alert_counts_by_name": dict(sorted(counts.items())[:20]),
        "background": {
            k: v for k, v in counts.items() if k.casefold() in {"watchdog", "infoinhibitor"}
        },
    }


__all__ = [
    "E9_SEMANTIC_OPERATIONS",
    "E9_SEMANTIC_OPERATION_SPECS",
    "E9SemanticOperations",
    "SemanticCapabilityResolver",
    "SemanticOperationSpec",
]
