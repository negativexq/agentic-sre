"""Ground-truth-free ITBench context, alert normalization and topology view."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

ITBENCH_EXTERNAL_CONTEXT_VERSION = "itbench_external_context_v2"
ITBENCH_EXTERNAL_CONTEXT_V3 = "itbench_external_context_v3"
MAX_EXTERNAL_CONTEXT_CHARS = 70_000
MAX_EXTERNAL_CONTEXT_V3_CHARS = 35_000


def normalize_alerts(backend: ITBenchSnapshotBackend) -> tuple[dict[str, Any], ...]:
    """Group repeated observable alert snapshots without consulting evaluator data."""
    groups: dict[str, dict[str, Any]] = {}
    for item in backend.complete_source_records(ITBenchEvidenceCategory.ALERTS):
        record = item.get("record", {})
        if not isinstance(record, dict):
            continue
        labels_raw = record.get("labels")
        labels: dict[str, Any] = labels_raw if isinstance(labels_raw, dict) else {}
        annotations_raw = record.get("annotations")
        annotations: dict[str, Any] = annotations_raw if isinstance(annotations_raw, dict) else {}
        name = labels.get("alertname", "unknown")
        stable_labels = {
            str(k): str(v) for k, v in labels.items() if k not in {"activeAt", "endsAt"}
        }
        signature = json.dumps([name, sorted(stable_labels.items())], separators=(",", ":"))
        timestamp = record.get("activeAt") or record.get("startsAt")
        group = groups.setdefault(
            signature,
            {
                "alert_identity": sha256(signature.encode()).hexdigest()[:16],
                "alertname": str(name),
                "labels": dict(sorted(stable_labels.items())),
                "annotations": {str(k): str(v) for k, v in sorted(annotations.items())},
                "first_observed": timestamp,
                "last_observed": timestamp,
                "occurrence_count": 0,
                "latest_state": record.get("state", "unknown"),
            },
        )
        group["occurrence_count"] += 1
        if isinstance(timestamp, str):
            if not isinstance(group["first_observed"], str) or timestamp < group["first_observed"]:
                group["first_observed"] = timestamp
            if not isinstance(group["last_observed"], str) or timestamp > group["last_observed"]:
                group["last_observed"] = timestamp
        group["latest_state"] = record.get("state", group["latest_state"])
    return tuple(
        sorted(groups.values(), key=lambda item: (item["alertname"], item["alert_identity"]))
    )


def build_external_context(
    backend: ITBenchSnapshotBackend,
    incident: Incident,
    alerts: tuple[Alert, ...],
    descriptors: tuple[dict[str, Any], ...],
    *,
    evidence: tuple[dict[str, Any], ...] = (),
    turn: int = 1,
    max_turns: int = 5,
    tool_calls_used: int = 0,
    tool_calls_limit: int = 12,
    case_state: dict[str, Any] | None = None,
    candidate_entities: tuple[dict[str, Any], ...] = (),
) -> str:
    """Build a complete-object JSON context with an explicit size guard."""
    payload = {
        "benchmark": "ITBench-Lite",
        "domain": "SRE",
        "context_version": ITBENCH_EXTERNAL_CONTEXT_VERSION,
        "incident": {
            "incident_id": str(incident.incident_id),
            "title": incident.title,
            "severity": incident.severity.value,
            "created_at": incident.created_at.isoformat(),
            "updated_at": incident.updated_at.isoformat(),
        },
        "alerts": list(normalize_alerts(backend)),
        "available_evidence": [category.value for category in ITBenchEvidenceCategory],
        "observable_entity_count": len(backend.observable_entities()),
        "candidate_shortlist": list(candidate_entities),
        "observable_topology": list(backend.topology(limit=100)),
        "evidence": list(evidence[-12:]),
        "case_state": case_state
        or {
            "queries_already_run": [],
            "entities_contextualized": [],
            "active_candidates": [],
        },
        "tool_catalog": list(descriptors),
        "execution": {
            "turn": turn,
            "max_turns": max_turns,
            "tool_calls_used": tool_calls_used,
            "tool_calls_remaining": max(tool_calls_limit - tool_calls_used, 0),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) > MAX_EXTERNAL_CONTEXT_CHARS:
        # The only potentially large model-facing field is observable topology.
        payload["observable_topology"] = list(backend.topology(limit=20))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) > MAX_EXTERNAL_CONTEXT_CHARS:
        raise ValueError("external investigator context exceeds configured bound")
    return encoded


_V3_SECTION_BUDGETS = {
    "incident": 1_500,
    "alert_digest": 4_000,
    "candidate_shortlist": 4_000,
    "relevant_topology": 4_000,
    "case_state": 5_000,
    "evidence": 10_000,
    "tool_hints": 2_000,
    "execution": 1_000,
}


def _fit_section(value: Any, budget: int) -> Any:
    """Bound one context section without cutting serialized JSON mid-field."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= budget:
        return value
    if isinstance(value, list):
        selected_items: list[Any] = []
        for item in value:
            candidate = selected_items + [item]
            if (
                len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str))
                > budget
            ):
                break
            selected_items.append(item)
        return {
            "items": selected_items,
            "source_count": len(value),
            "returned_count": len(selected_items),
            "truncated": len(selected_items) < len(value),
        }
    if isinstance(value, dict):
        return _fit_mapping(value, budget)
    return str(value)[: max(0, budget - 32)]


def _fit_mapping(value: dict[str, Any], budget: int) -> dict[str, Any]:
    """Pack dictionary children independently without re-embedding the source.

    A large first child must not erase all later semantic fields.  The returned
    wrapper is itself bounded and carries only the children that fit.
    """
    metadata = {
        "source_key_count": len(value),
        "returned_key_count": 0,
        "section_truncated": True,
    }
    selected: dict[str, Any] = {}
    for key, item in value.items():
        remaining = max(64, budget - len(json.dumps({"items": selected, **metadata})))
        child_budget = max(48, min(1_200, remaining))
        packed = _fit_value(item, child_budget)
        candidate = {
            "items": {**selected, str(key): packed},
            **metadata,
            "returned_key_count": len(selected) + 1,
        }
        if (
            len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str))
            > budget
        ):
            continue
        selected[str(key)] = packed
    result = {
        "items": selected,
        "source_key_count": len(value),
        "returned_key_count": len(selected),
        "section_truncated": len(selected) < len(value),
    }
    return result


def _fit_value(value: Any, budget: int) -> Any:
    """Recursively bound one semantic value to a deterministic character budget."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= budget:
        return value
    if isinstance(value, dict):
        return _fit_mapping(value, budget)
    if isinstance(value, list):
        selected_items: list[Any] = []
        for item in value:
            # Reserve enough room for a few representative items.  Dividing by
            # the complete source length made large alert lists discard every
            # item because each nested object could not fit its metadata.
            item_budget = max(48, min(300, budget // max(1, min(len(value), 4))))
            packed = _fit_value(item, item_budget)
            candidate = {
                "items": [*selected_items, packed],
                "source_count": len(value),
                "returned_count": len(selected_items) + 1,
                "truncated": True,
            }
            if (
                len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str))
                > budget
            ):
                break
            selected_items.append(packed)
        return {
            "items": selected_items,
            "source_count": len(value),
            "returned_count": len(selected_items),
            "truncated": len(selected_items) < len(value),
        }
    return str(value)[: max(1, budget - 2)]


def _fit_alert_digest(value: dict[str, Any], budget: int) -> dict[str, Any]:
    """Pack alert digest fields independently in semantic priority order."""
    # Keep a bounded slice for every digest dimension.  The proportions are
    # intentionally independent of source ordering so a large alert list
    # cannot erase services, namespaces, counts, or background context.
    proportions = {
        "high_signal_alerts": 0.42,
        "affected_services": 0.16,
        "affected_namespaces": 0.13,
        "alert_counts_by_name": 0.19,
        "background": 0.07,
    }
    available = max(320, int(budget * 0.45))
    field_budgets = {
        key: max(64, int(available * fraction / 0.97)) for key, fraction in proportions.items()
    }
    packed: dict[str, Any] = {}
    for key in (
        "high_signal_alerts",
        "affected_services",
        "affected_namespaces",
        "alert_counts_by_name",
        "background",
    ):
        if key in value:
            packed[key] = _fit_alert_value(value[key], field_budgets[key])
    if len(json.dumps(packed, ensure_ascii=False, separators=(",", ":"), default=str)) <= budget:
        return packed
    # The per-field allocations normally make this unnecessary.  Keep a final
    # deterministic fallback that retains field names and never emits an
    # unbounded source child.
    return {
        key: _fit_alert_value(item, max(32, budget // max(1, len(packed))))
        for key, item in packed.items()
    }


def _fit_alert_value(value: Any, budget: int) -> Any:
    """Compact alert values while retaining representative semantic fields."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= budget:
        return value
    if isinstance(value, list):
        selected_items: list[Any] = []
        for item in value:
            item_budget = max(32, min(240, budget // max(1, min(len(value), 4))))
            compact = _fit_alert_value(item, item_budget)
            candidate_items = [*selected_items, compact]
            if (
                len(
                    json.dumps(
                        candidate_items, ensure_ascii=False, separators=(",", ":"), default=str
                    )
                )
                > budget
            ):
                break
            selected_items.append(compact)
        return selected_items
    if isinstance(value, dict):
        selected_fields: dict[str, Any] = {}
        for key, item in value.items():
            compact = _fit_alert_value(item, max(32, budget // max(1, len(value))))
            candidate_fields = {**selected_fields, str(key): compact}
            if (
                len(
                    json.dumps(
                        candidate_fields, ensure_ascii=False, separators=(",", ":"), default=str
                    )
                )
                > budget
            ):
                continue
            selected_fields[str(key)] = compact
        return selected_fields
    return str(value)[: max(1, budget - 2)]


def _alert_digest(backend: ITBenchSnapshotBackend) -> dict[str, Any]:
    grouped = list(normalize_alerts(backend))
    alert_counts: dict[str, int] = {}
    affected_services: set[str] = set()
    affected_namespaces: set[str] = set()
    high_signal: list[dict[str, Any]] = []
    background: dict[str, int] = {}
    for item in grouped:
        name = str(item.get("alertname", "unknown"))
        labels = item.get("labels", {}) if isinstance(item.get("labels"), dict) else {}
        count = int(item.get("occurrence_count", 0) or 0)
        alert_counts[name] = alert_counts.get(name, 0) + count
        for key in ("service", "service_name", "app", "workload"):
            if isinstance(labels.get(key), str):
                affected_services.add(labels[key])
        if isinstance(labels.get("namespace"), str):
            affected_namespaces.add(labels["namespace"])
        severity = str(labels.get("severity", ""))
        if name.casefold() in {"watchdog", "infoinhibitor"} or severity.casefold() == "info":
            background[name] = background.get(name, 0) + count
            continue
        high_signal.append(
            {
                "alertname": name,
                "namespace": labels.get("namespace"),
                "service_or_entity": labels.get("service")
                or labels.get("entity")
                or labels.get("app"),
                "severity": severity or None,
                "occurrence_count": count,
                "first_observed": item.get("first_observed"),
                "last_observed": item.get("last_observed"),
            }
        )
    return {
        "high_signal_alerts": high_signal[:20],
        "affected_services": sorted(affected_services)[:50],
        "affected_namespaces": sorted(affected_namespaces)[:50],
        "alert_counts_by_name": dict(sorted(alert_counts.items())),
        "background": dict(sorted(background.items())),
    }


def _relevant_topology(
    backend: ITBenchSnapshotBackend,
    candidate_entities: tuple[dict[str, Any], ...],
    case_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    entities: list[str] = []
    for item in candidate_entities:
        canonical = item.get("canonical") or item.get("entity")
        if isinstance(canonical, str):
            entities.append(canonical)
    for key in ("active_candidates", "supported_candidates"):
        for entity in (case_state or {}).get(key, []):
            if isinstance(entity, str):
                entities.append(entity)
    wanted = set(entities)
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    # The candidate ranker has already built the complete observable edge set for
    # the first turn. Filter that complete set before applying this response bound;
    # do not re-derive the entire topology once per candidate.
    for edge in backend.topology(limit=None):
        if wanted and not ({edge.get("source"), edge.get("target")} & wanted):
            continue
        edge_key = (str(edge.get("source")), str(edge.get("target")), str(edge.get("relationship")))
        result[edge_key] = edge
    return [result[edge_key] for edge_key in sorted(result)[:80]]


def _compact_case_state(case_state: dict[str, Any] | None) -> dict[str, Any]:
    state = case_state or {}
    return {
        "observed_symptoms": list(state.get("observed_symptoms", []))[:8],
        "active_candidates": list(state.get("active_candidates", []))[:3],
        "supported_candidates": list(state.get("supported_candidates", []))[:10],
        "rejected_candidates": list(state.get("rejected_candidates", []))[:10],
        "evidence_by_candidate": state.get("evidence_by_candidate", {}),
        "contradictions_by_candidate": state.get("contradictions_by_candidate", {}),
        "queries_already_run": list(state.get("queries_already_run", []))[-12:],
        "budget": state.get("budget", {}),
    }


def build_external_context_v3(
    backend: ITBenchSnapshotBackend,
    incident: Incident,
    alerts: tuple[Alert, ...],
    descriptors: tuple[dict[str, Any], ...],
    *,
    evidence: tuple[dict[str, Any], ...] = (),
    turn: int = 1,
    max_turns: int = 5,
    tool_calls_used: int = 0,
    tool_calls_limit: int = 12,
    case_state: dict[str, Any] | None = None,
    candidate_entities: tuple[dict[str, Any], ...] = (),
) -> str:
    """Build compact V3 context from relevant, observable sections only."""
    hints = [
        {
            "name": item.get("name"),
            "purpose": item.get("purpose"),
            "arguments": sorted((item.get("arguments") or {}).keys()),
        }
        for item in descriptors
    ]
    sections: dict[str, Any] = {
        "incident": {
            "incident_id": str(incident.incident_id),
            "title": incident.title,
            "severity": incident.severity.value,
            "created_at": incident.created_at.isoformat(),
            "updated_at": incident.updated_at.isoformat(),
        },
        "alert_digest": _fit_alert_digest(
            _alert_digest(backend), _V3_SECTION_BUDGETS["alert_digest"]
        ),
        "candidate_shortlist": list(candidate_entities)[:10],
        "relevant_topology": _relevant_topology(backend, candidate_entities, case_state),
        "case_state": _compact_case_state(case_state),
        "evidence": list(evidence[-10:]),
        "tool_hints": hints,
        "execution": {
            "turn": turn,
            "max_turns": max_turns,
            "semantic_tool_executions_used": tool_calls_used,
            "semantic_tool_executions_remaining": max(tool_calls_limit - tool_calls_used, 0),
        },
    }
    payload: dict[str, Any] = {
        "benchmark": "ITBench-Lite",
        "domain": "SRE",
        "context_version": ITBENCH_EXTERNAL_CONTEXT_V3,
    }
    for name, value in sections.items():
        payload[name] = _fit_section(value, _V3_SECTION_BUDGETS[name])
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) > MAX_EXTERNAL_CONTEXT_V3_CHARS:
        # Reduce only low-priority history/evidence sections; never remove identity or digest.
        payload["evidence"] = _fit_section(list(evidence[-5:]), 5_000)
        payload["case_state"] = _fit_section(_compact_case_state(case_state), 3_500)
        payload["relevant_topology"] = _fit_section(payload["relevant_topology"], 2_500)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) > MAX_EXTERNAL_CONTEXT_V3_CHARS:
        raise ValueError("external investigator V3 context exceeds configured bound")
    return encoded


def context_hash(context: str) -> str:
    """Hash the exact model-visible external context for dry-run provenance."""
    return sha256(context.encode("utf-8")).hexdigest()


__all__ = [
    "ITBENCH_EXTERNAL_CONTEXT_VERSION",
    "ITBENCH_EXTERNAL_CONTEXT_V3",
    "MAX_EXTERNAL_CONTEXT_CHARS",
    "MAX_EXTERNAL_CONTEXT_V3_CHARS",
    "build_external_context",
    "build_external_context_v3",
    "context_hash",
    "normalize_alerts",
]
