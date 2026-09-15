"""High-level diagnostic operations for E9."""

from __future__ import annotations

import json
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.e9_memory import E9CaseMemory
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
    """Map diagnostic questions to bounded, typed snapshot reads."""

    def __init__(self, backend: ITBenchSnapshotBackend, memory: E9CaseMemory) -> None:
        self.backend = backend
        self.memory = memory

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
            data = {
                "alerts": _alert_digest(normalize_alerts(self.backend)),
                "candidates": list(self.memory.state["discovered_entities"].values())[:10],
                "topology": list(self.backend.topology(limit=16)),
            }
            category = "incident"
        elif operation == "ALERT_ANALYSIS":
            data = self.backend.query(ITBenchEvidenceCategory.ALERTS, {"limit": 20})
            category = "alerts"
        elif operation == "TOPOLOGY_ANALYSIS":
            data = {
                "edges": list(self.backend.topology(entity=canonical, limit=20))
                if canonical
                else list(self.backend.topology(limit=20))
            }
            category = "topology"
        elif operation in {"ENTITY_CONTEXT", "SPEC_ANALYSIS", "COMPARE_REPLICAS"}:
            if canonical is None:
                raise ValueError("entity context requires a candidate handle")
            data = self.backend.query_entity_context(canonical, 8, include_telemetry=False)
            if operation == "COMPARE_REPLICAS":
                data = {"entity": canonical, "comparison": _replica_comparison(data)}
            category = "entity_context"
        elif operation == "EVENT_ANALYSIS":
            data = self.backend.query(
                ITBenchEvidenceCategory.K8S_EVENTS, {"entity": canonical, "limit": 20}
            )
            category = "events"
        elif operation == "METRIC_ANOMALIES":
            service = canonical.rsplit("/", 1)[-1] if canonical else None
            data = self.backend.metric_analysis(
                {"service": service, "limit": 12} if service else {"limit": 12}
            )
            category = "metrics"
        elif operation == "TRACE_ERROR_TREE":
            service = canonical.rsplit("/", 1)[-1] if canonical else None
            data = self.backend.query(
                ITBenchEvidenceCategory.TRACES,
                {"service": service, "limit": 12} if service else {"limit": 12},
            )
            category = "traces"
        else:
            if canonical is None:
                raise ValueError("temporal verification requires a candidate handle")
            data = {
                "entity": canonical,
                "events": self.backend.query(
                    ITBenchEvidenceCategory.K8S_EVENTS, {"entity": canonical, "limit": 10}
                ),
                "context": self.backend.query_entity_context(canonical, 6, include_telemetry=False),
            }
            category = "temporal"
        summary = _bounded(data)
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


def _bounded(value: Any, limit: int = 5_500) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    return {"summary": encoded[: limit - 60], "truncated": True}


def _alert_digest(alerts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
        "high_signal_alerts": [
            item
            for item in alerts
            if str(item.get("alertname", "")).casefold() not in {"watchdog", "infoinhibitor"}
        ][:12],
        "alert_count": len(alerts),
    }


def _replica_comparison(context: dict[str, Any]) -> dict[str, Any]:
    objects = context.get("object_state", [])
    return {
        "available": bool(objects),
        "object_state_samples": objects[:8] if isinstance(objects, list) else [],
    }


__all__ = ["E9_SEMANTIC_OPERATIONS", "E9SemanticOperations"]
