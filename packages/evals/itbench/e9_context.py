"""Phase-aware, bounded E9 context projection."""

from __future__ import annotations

import json
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_fsm import E9FSM
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_packing import bounded_pack
from packages.evals.itbench.e9_semantic import SemanticCapabilityResolver
from packages.evals.itbench.external_context import normalize_alerts
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

E9_CONTEXT_VERSION = "itbench_external_context_v4"
E9_CONTEXT_MAX_CHARS = 25_000


class E9ContextPlanner:
    """Select only current-question evidence and workflow state."""

    def plan(
        self,
        backend: ITBenchSnapshotBackend,
        incident: Incident,
        alerts: tuple[Alert, ...],
        memory: E9CaseMemory,
        fsm: E9FSM | None,
        *,
        turn: int,
        max_steps: int,
        semantic_limit: int,
        visible_rejection_feedback: bool = True,
        dynamic_action_gating: bool = True,
        dynamic_operation_gating: bool = True,
        selective_context: bool = True,
    ) -> tuple[str, dict[str, int]]:
        candidates = backend.candidate_entities(limit=10)
        memory.discover_entities(candidates, turn=turn)
        target = memory.state.get("current_hypothesis", {})
        target_handle = target.get("entity_handle") if isinstance(target, dict) else None
        handles = list(memory.state["discovered_entities"].values())
        handles.sort(
            key=lambda item: (
                item.get("handle") != target_handle,
                -int(item.get("discovered_turn", 0)),
                str(item.get("handle")),
            )
        )
        handles = handles[:10]
        alert_items = normalize_alerts(backend)
        digest = _alert_digest(alert_items)
        available_operations = SemanticCapabilityResolver(
            backend, memory, incident
        ).available_operations(
            phase=str(memory.state.get("current_phase", "OBSERVE")), target_handle=target_handle
        )
        surface = control_surface(
            memory.state,
            turn=turn,
            max_steps=max_steps,
            max_rejections=(fsm.max_consecutive_rejections if fsm else 2),
            semantic_limit=semantic_limit,
            available_operations=available_operations,
        )
        topology: list[dict[str, Any]] = []
        if isinstance(target_handle, str):
            canonical = memory.resolve(target_handle)
            if canonical:
                topology = list(backend.topology(entity=canonical, limit=12))
        if not topology:
            for item in handles[:3]:
                topology.extend(backend.topology(entity=item["canonical"], limit=4))
        case_state = memory.projection()
        case_state.pop("discovered_entities", None)
        if not visible_rejection_feedback:
            case_state.pop("last_rejection", None)
        payload: dict[str, Any] = {
            "context_version": E9_CONTEXT_VERSION,
            "incident": {
                "title": incident.title,
                "severity": incident.severity.value,
                "created_at": incident.created_at.isoformat(),
                "updated_at": incident.updated_at.isoformat(),
            },
            "alert_digest": digest,
            "candidate_shortlist": [
                {
                    "handle": item["handle"],
                    "canonical": item["canonical"],
                    **item.get("metadata", {}),
                }
                for item in handles
            ],
            "relevant_topology": topology[:16],
            "case_state": case_state,
            "workflow": {
                "phase": surface.phase,
                "turn": turn,
                "model_steps_remaining": max(max_steps - turn, 0),
                "semantic_actions_remaining": max(
                    semantic_limit - int(memory.state["semantic_actions_used"]), 0
                ),
                "rejection_budget_remaining": surface.rejection_budget_remaining,
                "actions": {
                    action: {
                        "targets": list(targets),
                        "operations": list(operations),
                    }
                    for action, targets, operations in surface.action_capabilities
                },
            },
            "rules": [
                "Candidate handles are runtime-owned; do not invent C### handles.",
                "Evidence provenance is runtime-owned; do not copy evidence handles into SUBMIT.",
                "contains is a literal substring, not regex or a query language.",
                "Use one discriminating operation for the current causal question.",
                "The runtime can reject safe actions and provide valid next actions.",
                "A rejected action is explained in last_rejection; do not repeat it unchanged.",
                "Submit the smallest independently causal candidate set or STOP.",
            ],
        }
        if not dynamic_action_gating:
            payload["workflow"]["actions"] = {
                action: {
                    "targets": list(surface.target_handles),
                    "operations": list(surface.operations),
                }
                for action in (
                    "OBSERVE",
                    "HYPOTHESIZE",
                    "INVESTIGATE",
                    "REVISE",
                    "SUBMIT",
                    "STOP",
                )
            }
        if not dynamic_operation_gating:
            for action in payload["workflow"]["actions"].values():
                action["operations"] = list(
                    (
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
                )
        if not selective_context:
            payload["case_state"]["evidence"] = list(memory.state["evidence"].values())
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > E9_CONTEXT_MAX_CHARS:
            payload["case_state"]["evidence"] = payload["case_state"].get("evidence", [])[-6:]
            payload["case_state"]["operations_already_run"] = payload["case_state"].get(
                "operations_already_run", []
            )[-8:]
            payload["relevant_topology"] = topology[:8]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > E9_CONTEXT_MAX_CHARS:
            raise ValueError("E9 ContextPlanner exceeded bounded context")
        sections = {
            key: len(json.dumps(value, ensure_ascii=False, default=str))
            for key, value in payload.items()
        }
        sections["total"] = len(encoded)
        return encoded, sections


def _alert_digest(alerts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    services: set[str] = set()
    namespaces: set[str] = set()
    high_signal: list[dict[str, Any]] = []
    for item in alerts:
        name = str(item.get("alertname", "unknown"))
        counts[name] = counts.get(name, 0) + int(item.get("occurrence_count", 1) or 1)
        labels = item.get("labels", {})
        if isinstance(labels, dict):
            for key in ("service", "service_name", "app", "workload"):
                if isinstance(labels.get(key), str):
                    services.add(labels[key])
            if isinstance(labels.get("namespace"), str):
                namespaces.add(labels["namespace"])
        if name.casefold() not in {"watchdog", "infoinhibitor"}:
            high_signal.append(
                {
                    "alertname": name,
                    "severity": item.get("labels", {}).get("severity")
                    if isinstance(item.get("labels"), dict)
                    else None,
                    "occurrence_count": item.get("occurrence_count", 1),
                    "first_observed": item.get("first_observed"),
                    "last_observed": item.get("last_observed"),
                }
            )
    return _fit_alert_digest(
        {
            "high_signal_alerts": high_signal[:12],
            "affected_services": sorted(services)[:20],
            "affected_namespaces": sorted(namespaces)[:20],
            "alert_counts_by_name": dict(sorted(counts.items())[:20]),
            "background": {
                key: value for key, value in counts.items() if key in {"Watchdog", "InfoInhibitor"}
            },
        }
    )


def _fit_alert_digest(value: dict[str, Any], limit: int = 4_000) -> dict[str, Any]:
    """Pack alert sections independently so one noisy list cannot erase all signal."""
    packed: dict[str, Any] = {}
    for key in (
        "high_signal_alerts",
        "affected_services",
        "affected_namespaces",
        "alert_counts_by_name",
        "background",
    ):
        child = value.get(key, [] if key != "alert_counts_by_name" and key != "background" else {})
        child_limit = max(180, limit // 5)
        child_packed = bounded_pack(child, child_limit)
        packed[key] = child_packed
    return packed


__all__ = ["E9_CONTEXT_MAX_CHARS", "E9_CONTEXT_VERSION", "E9ContextPlanner"]
