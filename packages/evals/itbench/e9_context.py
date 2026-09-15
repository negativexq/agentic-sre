"""Phase-aware, bounded E9 context projection."""

from __future__ import annotations

import json
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_fsm import E9FSM
from packages.evals.itbench.e9_memory import E9CaseMemory
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
    ) -> tuple[str, dict[str, int]]:
        candidates = backend.candidate_entities(limit=10)
        memory.discover_entities(candidates)
        handles = list(memory.state["discovered_entities"].values())[:10]
        alert_items = normalize_alerts(backend)
        digest = _alert_digest(alert_items)
        surface = control_surface(
            memory.state,
            turn=turn,
            max_steps=max_steps,
            max_rejections=(fsm.max_consecutive_rejections if fsm else 2),
            semantic_limit=semantic_limit,
        )
        topology: list[dict[str, Any]] = []
        target = memory.state.get("current_hypothesis", {})
        target_handle = target.get("entity_handle") if isinstance(target, dict) else None
        if isinstance(target_handle, str):
            canonical = memory.resolve(target_handle)
            if canonical:
                topology = list(backend.topology(entity=canonical, limit=12))
        if not topology:
            for item in handles[:3]:
                topology.extend(backend.topology(entity=item["canonical"], limit=4))
        case_state = memory.projection()
        case_state.pop("discovered_entities", None)
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
                "valid_actions": surface.actions,
                "turn": turn,
                "model_steps_remaining": max(max_steps - turn, 0),
                "semantic_actions_remaining": max(
                    semantic_limit - int(memory.state["semantic_actions_used"]), 0
                ),
                "rejection_budget_remaining": surface.rejection_budget_remaining,
                "valid_operations": surface.operations,
                "target_handles": surface.target_handles,
            },
            "semantic_operations": surface.operations,
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
    return {
        "high_signal_alerts": high_signal[:12],
        "affected_services": sorted(services)[:20],
        "affected_namespaces": sorted(namespaces)[:20],
        "alert_counts_by_name": dict(sorted(counts.items())[:20]),
        "background": {
            key: value for key, value in counts.items() if key in {"Watchdog", "InfoInhibitor"}
        },
    }


def _operations_for_phase(phase: str, final_turn: bool) -> tuple[str, ...]:
    if final_turn:
        return ("SUBMIT", "REVISE", "STOP")
    if phase == "OBSERVE":
        return (
            "INCIDENT_OVERVIEW",
            "ALERT_ANALYSIS",
            "TOPOLOGY_ANALYSIS",
            "RECENT_CHANGE_ANALYSIS",
            "EVENT_ANALYSIS",
        )
    if phase == "VERIFY":
        return (
            "ENTITY_CONTEXT",
            "METRIC_ANOMALIES",
            "TRACE_ERROR_TREE",
            "SPEC_ANALYSIS",
            "COMPARE_REPLICAS",
            "VERIFY_TEMPORAL_ALIGNMENT",
        )
    return ("INCIDENT_OVERVIEW", "ENTITY_CONTEXT", "EVENT_ANALYSIS", "VERIFY_TEMPORAL_ALIGNMENT")


__all__ = ["E9_CONTEXT_MAX_CHARS", "E9_CONTEXT_VERSION", "E9ContextPlanner"]
