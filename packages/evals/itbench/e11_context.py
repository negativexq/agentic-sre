"""Bounded, ground-truth-free E11 model context assembly."""

from __future__ import annotations

import json
from typing import Any

from packages.evals.itbench.e11_observability import RankedCandidate

E11_CONTEXT_VERSION = "itbench_e11_investigation_context_v3"
E11_CONTEXT_MAX_CHARS = 20_000
E11_CONTEXT_FIELDS = (
    "current_hypothesis",
    "candidate_state",
    "supporting_evidence",
    "contradicting_evidence",
    "inconclusive_evidence",
    "recent_evidence",
    "recent_operations",
    "unresolved_question",
    "alternative_candidates",
    "last_rejection",
    "ranking_revisions",
    "legal_next_actions",
    "legal_target_operations",
)


def build_e11_context(
    *,
    incident: dict[str, Any],
    candidates: tuple[RankedCandidate, ...],
    phase: str,
    remaining_model_calls: int,
    remaining_semantic_actions: int,
    topology: tuple[dict[str, Any], ...] = (),
    investigation_state: dict[str, Any] | None = None,
    observation_evidence: tuple[dict[str, Any], ...] = (),
    legal_next_actions: tuple[str, ...] = (),
    legal_target_operations: dict[str, tuple[str, ...]] | None = None,
    max_chars: int = E11_CONTEXT_MAX_CHARS,
) -> str:
    """Build bounded context while preserving actual semantic result content."""
    if max_chars < 1:
        raise ValueError("context bound must be positive")
    state = _normalize_investigation_state(investigation_state or {})
    state["recent_evidence"] = _bounded_evidence(
        observation_evidence or state.get("recent_evidence", ())
    )
    state["legal_next_actions"] = list(legal_next_actions)
    state["legal_target_operations"] = {
        str(handle): list(operations)
        for handle, operations in (legal_target_operations or {}).items()
    }
    packet: dict[str, Any] = {
        "context_version": E11_CONTEXT_VERSION,
        "phase": phase,
        "remaining_model_calls": remaining_model_calls,
        "remaining_semantic_actions": remaining_semantic_actions,
        "incident": _bounded_incident(incident),
        "active_candidates": [candidate.as_dict() for candidate in candidates],
        "investigation": state,
        "directed_topology": list(topology[:24]),
    }
    encoded = _encode(packet)
    if len(encoded) <= max_chars:
        return encoded
    # Drop low-priority topology and retrieval detail first.  Evidence remains.
    packet["directed_topology"] = []
    for candidate in packet["active_candidates"]:
        candidate["retrieval_evidence"] = candidate["retrieval_evidence"][:3]
    encoded = _encode(packet)
    if len(encoded) > max_chars:
        state = packet["investigation"]
        state["alternative_candidates"] = state.get("alternative_candidates", [])[-3:]
        state["ranking_revisions"] = state.get("ranking_revisions", [])[-2:]
        state["recent_operations"] = state.get("recent_operations", [])[-3:]
        state["inconclusive_evidence"] = state.get("inconclusive_evidence", [])[-3:]
        packet["active_candidates"] = packet["active_candidates"][:6]
        encoded = _encode(packet)
    if len(encoded) > max_chars:
        state = packet["investigation"]
        compact_state = {
            key: state.get(key)
            for key in (
                "current_hypothesis",
                "candidate_state",
                "supporting_evidence",
                "contradicting_evidence",
                "inconclusive_evidence",
                "recent_evidence",
                "unresolved_question",
                "last_rejection",
                "legal_next_actions",
                "legal_target_operations",
            )
            if state.get(key) not in (None, [], {}, "")
        }
        packet = {
            "context_version": E11_CONTEXT_VERSION,
            "phase": phase,
            "remaining_model_calls": remaining_model_calls,
            "remaining_semantic_actions": remaining_semantic_actions,
            "incident": _bounded_incident(incident),
            "active_candidates": packet["active_candidates"][:3],
            "investigation": compact_state,
        }
        encoded = _encode(packet)
    if len(encoded) > max_chars:
        raise ValueError("minimal E11 context exceeds the configured bound")
    return encoded


def _normalize_investigation_state(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize E9's historical projection names to the E11 contract."""
    aliases = {
        "candidate_status": "candidate_state",
        "unresolved_questions": "unresolved_question",
        "ranking_history": "ranking_revisions",
    }
    result: dict[str, Any] = {}
    for key in E11_CONTEXT_FIELDS:
        source = next((name for name, target in aliases.items() if target == key), key)
        item = value.get(key, value.get(source))
        if item is None and key == "recent_evidence":
            item = value.get("evidence", ())
        if item is not None:
            result[key] = _bounded_item(item, 2_000 if key.endswith("evidence") else 900)
    result["candidate_state"] = _bounded_item(result.get("candidate_state", {}), 2_000)
    result["alternative_candidates"] = _bounded_item(value.get("alternative_candidates", []), 900)
    return result


def _bounded_incident(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "scenario_id",
        "incident_id",
        "title",
        "description",
        "incident_start",
        "observation_window",
        "affected_identities",
        "symptoms",
        "diagnostic_alert_group_count",
        "background_alert_counts",
    )
    selected = {key: value[key] for key in keys if key in value}
    bounded = _bounded_item(selected or value, 2_400)
    result = bounded if isinstance(bounded, dict) else {"summary": str(bounded)}
    alerts = value.get("diagnostic_alerts")
    if isinstance(alerts, list):
        # Alerts are the primary symptom signal; bound them by count, not by a
        # share of the incident budget.
        result["diagnostic_alerts"] = [
            {
                key: item[key]
                for key in (
                    "alert_name",
                    "service",
                    "namespace",
                    "first_starts_at",
                    "last_starts_at",
                    "starts_at",
                    "occurrence_count",
                )
                if key in item
            }
            for item in alerts[:12]
            if isinstance(item, dict)
        ]
    return result


def _bounded_evidence(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_bounded_item(item, 1_600) for item in value[-8:]]


def _bounded_item(value: Any, limit: int) -> Any:
    if isinstance(value, dict):
        if "evidence_ref" in value and "finding" in value:
            finding = value.get("finding", {})
            if isinstance(finding, dict):
                finding = _bounded_finding(finding, 1_000)
            return {
                key: _bounded_item(value.get(key), 180)
                for key in ("evidence_ref", "operation", "target", "result_status")
                if key in value
            } | {"finding": finding}
        if "records" in value and isinstance(value.get("records"), list):
            result = {
                key: value[key]
                for key in ("category", "matching_count", "result_status")
                if key in value
            }
            result["records"] = [_bounded_record(item) for item in value["records"][:6]]
            return result
        result = {
            str(key): _bounded_item(item, max(80, limit // max(2, len(value))))
            for key, item in value.items()
        }
        # Drop whole trailing keys instead of cutting serialized JSON mid-value.
        dropped = 0
        while (
            result
            and len(_encode({**result, "truncated_keys": dropped} if dropped else result)) > limit
        ):
            result.pop(next(reversed(result)))
            dropped += 1
        if dropped:
            result["truncated_keys"] = dropped
        return result
    if isinstance(value, (list, tuple)):
        list_result = [
            _bounded_item(item, max(80, limit // max(2, len(value)))) for item in value[-12:]
        ]
        # Keep the most recent entries that fit.
        while list_result and len(_encode(list_result)) > limit:
            list_result.pop(0)
        return list_result
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if len(str(value)) <= limit else str(value)[:limit]
    return str(value)[:limit]


def _bounded_record(value: Any) -> Any:
    if not isinstance(value, dict):
        return str(value)[:500]
    keys = (
        "evidence_id",
        "alert_name",
        "alertname",
        "labels",
        "occurrence_count",
        "reason",
        "type",
        "message",
        "timestamp",
        "namespace",
        "service",
        "diagnostic_class",
        "pattern",
        "count",
        "classification_reason",
        "source_service",
        "destination_service",
        "status",
        "finding",
        "rule_status",
        "relation",
        "candidate",
        "affected_entity",
        "configuration_dependency_match",
        "selector_target_match",
        "chaos_target_match",
        "timing_compatible",
    )
    return {key: value[key] for key in keys if key in value}


def _bounded_finding(value: dict[str, Any], limit: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"records", "patterns", "edges", "causal_findings"} and isinstance(item, list):
            result[key] = [_bounded_record(entry) for entry in item[:6]]
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif (
            isinstance(item, list)
            and item
            and all(isinstance(entry, (str, int, float, bool)) for entry in item)
        ):
            result[key] = item[:12]
        elif key in {"alerts", "high_signal_alerts"} and isinstance(item, list):
            result[key] = [_bounded_record(entry) for entry in item[:6]]
        elif key == "alerts" and isinstance(item, dict):
            result[key] = {
                subkey: [_bounded_record(entry) for entry in subvalue[:6]]
                if isinstance(subvalue, list)
                else subvalue
                for subkey, subvalue in item.items()
                if isinstance(subvalue, (list, str, int, float, bool)) or subvalue is None
            }
    return (
        result if len(_encode(result)) <= limit else {key: result[key] for key in list(result)[:8]}
    )


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "E11_CONTEXT_FIELDS",
    "E11_CONTEXT_MAX_CHARS",
    "E11_CONTEXT_VERSION",
    "build_e11_context",
]
