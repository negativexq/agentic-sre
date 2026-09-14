"""Observable ITBench incident construction without ground truth."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

_INCIDENT_NAMESPACE = UUID("1a7ad602-64db-4c7e-bb7c-f91e2d53cc4f")


def build_observable_incident(
    backend: ITBenchSnapshotBackend,
) -> tuple[Incident, tuple[Alert, ...]]:
    """Build alert context from snapshot observations only."""
    # Alert snapshots are small relative to raw telemetry.  Scan all alert
    # files so incident construction does not accidentally depend on the
    # bounded model-facing record window.
    alert_items = backend.complete_source_records(ITBenchEvidenceCategory.ALERTS)
    normalized: list[Alert] = []
    for index, item in enumerate(alert_items):
        record = item.get("record", {})
        if not isinstance(record, dict) or record.get("state") != "firing":
            continue
        labels = record.get("labels")
        annotations = record.get("annotations")
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            continue
        name = labels.get("alertname")
        if not isinstance(name, str) or name == "Watchdog":
            continue
        start = _timestamp(record.get("activeAt"))
        if start is None:
            continue
        service = str(labels.get("service_name", labels.get("service", "unknown")))
        namespace = str(labels.get("namespace", "unknown"))
        fingerprint = f"{backend.scenario.scenario_id}:{name}:{service}:{index}"
        normalized.append(
            Alert(
                alert_name=name,
                service=service,
                namespace=namespace,
                cluster="itbench-lite",
                starts_at=start,
                labels={str(key): str(value) for key, value in labels.items()},
                annotations={str(key): str(value) for key, value in annotations.items()},
                fingerprint=fingerprint,
                status=AlertStatus.FIRING,
                source=AlertSource.PROMETHEUS,
            )
        )
    if not normalized:
        raise ValueError(f"no observable firing alert found for {backend.scenario.scenario_id}")
    earliest = min(item.starts_at for item in normalized)
    incident = Incident(
        incident_id=uuid5(_INCIDENT_NAMESPACE, backend.scenario.scenario_id),
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title=f"ITBench-Lite SRE incident {backend.scenario.scenario_id}",
        description="Snapshot-backed alert context; causal truth is evaluator-only.",
        created_at=earliest,
        updated_at=earliest,
        correlation_id=uuid5(_INCIDENT_NAMESPACE, f"correlation:{backend.scenario.scenario_id}"),
    )
    return incident, tuple(normalized)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["build_observable_incident"]
