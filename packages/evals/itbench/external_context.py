"""Ground-truth-free ITBench context, alert normalization and topology view."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

ITBENCH_EXTERNAL_CONTEXT_VERSION = "itbench_external_context_v2"
MAX_EXTERNAL_CONTEXT_CHARS = 70_000


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


def context_hash(context: str) -> str:
    """Hash the exact model-visible external context for dry-run provenance."""
    return sha256(context.encode("utf-8")).hexdigest()


__all__ = [
    "ITBENCH_EXTERNAL_CONTEXT_VERSION",
    "MAX_EXTERNAL_CONTEXT_CHARS",
    "build_external_context",
    "context_hash",
    "normalize_alerts",
]
