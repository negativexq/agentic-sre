"""Bounded semantic SRE operations over the typed snapshot backend."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from packages.contracts import Incident
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
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
    "METRIC_ANOMALIES",
    "TRACE_ERROR_TREE",
    "SPEC_ANALYSIS",
    "COMPARE_REPLICAS",
    "VERIFY_TEMPORAL_ALIGNMENT",
)


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
        elif operation == "METRIC_ANOMALIES":
            service = canonical.rsplit("/", 1)[-1] if canonical else None
            data, category = (
                self.backend.metric_analysis(
                    {"service": service, "limit": self._limit(12)}
                    if service
                    else {"limit": self._limit(12)}
                ),
                "metrics",
            )
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

    def _compare_replicas(self, canonical: str) -> dict[str, Any]:
        context = self.backend.query_entity_context(canonical, 20, include_telemetry=False)
        peers = []
        for item in context.get("object_records", []):
            record = item.get("record", {})
            body = _json_body(record.get("Body")) if isinstance(record, dict) else None
            peer = _canonical_from_body(body or {})
            if peer and peer != canonical:
                peers.append(item)
        return {
            "comparison_available": bool(peers),
            "peer_count": len(peers),
            "spec_differences": [],
            "event_differences": [],
            "reason": None
            if peers
            else "peer replica identities are not observable from this entity query",
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
    return workload_spec.get("resources", spec.get("resources"))


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


__all__ = ["E9_SEMANTIC_OPERATIONS", "E9SemanticOperations"]
