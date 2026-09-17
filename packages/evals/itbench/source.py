"""Observation source over one ITBench-Lite snapshot. Never reads ground truth."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory, ITBenchScenario
from packages.evals.itbench.dataset import iter_tsv
from packages.rca.json_access import child
from packages.rca.model import CLUSTER_SCOPE, Alert, ClusterEvent, EntityRef, ObjectVersion

_OBJECTS = "k8s_objects_raw.tsv"
_EVENTS = "k8s_events_raw.tsv"
_LOGS = "otel_logs_raw.tsv"


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _entity(kind: Any, name: Any, namespace: Any) -> EntityRef | None:
    if not isinstance(kind, str) or not isinstance(name, str) or not kind or not name:
        return None
    return EntityRef(
        kind=kind,
        name=name,
        namespace=namespace if isinstance(namespace, str) and namespace else CLUSTER_SCOPE,
    )


class SnapshotSource:
    """Reads alerts, object versions, and events from snapshot files."""

    def __init__(self, scenario: ITBenchScenario) -> None:
        self.scenario = scenario
        self.root = Path(scenario.snapshot_path)

    def incident_id(self) -> str:
        return self.scenario.scenario_id

    @cached_property
    def _alerts(self) -> list[Alert]:
        result: list[Alert] = []
        for relative in self.scenario.evidence_files.get(ITBenchEvidenceCategory.ALERTS, ()):
            value = json.loads((self.root / relative).read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value = value.get("data", value)
                value = value.get("alerts", []) if isinstance(value, dict) else value
            for item in value if isinstance(value, list) else []:
                if not isinstance(item, dict) or item.get("state") != "firing":
                    continue
                labels = item.get("labels")
                starts = parse_time(item.get("activeAt"))
                if not isinstance(labels, dict) or starts is None:
                    continue
                name = labels.get("alertname")
                if not isinstance(name, str):
                    continue
                result.append(
                    Alert(
                        name=name,
                        service=labels.get("service_name") or labels.get("service"),
                        namespace=labels.get("namespace"),
                        starts_at=starts,
                        labels={str(k): str(v) for k, v in labels.items()},
                    )
                )
        return result

    def alerts(self) -> Sequence[Alert]:
        return self._alerts

    @cached_property
    def _history(self) -> dict[EntityRef, list[ObjectVersion]]:
        history: dict[EntityRef, list[ObjectVersion]] = {}
        path = self.root / _OBJECTS
        for index, row in enumerate(iter_tsv(path)):
            try:
                body = json.loads(row.get("Body", ""))
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            metadata = body.get("metadata")
            if not isinstance(metadata, dict):
                continue
            ref = _entity(body.get("kind"), metadata.get("name"), metadata.get("namespace"))
            observed = parse_time(row.get("Timestamp"))
            if ref is None or observed is None:
                continue
            versions = history.setdefault(ref, [])
            content = {k: v for k, v in body.items() if k not in {"status", "metadata"}}
            if versions:
                last = versions[-1].body
                if {k: v for k, v in last.items() if k not in {"status", "metadata"}} == content:
                    continue
            versions.append(
                ObjectVersion(
                    entity=ref,
                    observed_at=observed,
                    body=body,
                    evidence_id=f"{_OBJECTS}:{index}",
                )
            )
        for versions in history.values():
            versions.sort(key=lambda item: item.observed_at)
        return history

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        return self._history

    @cached_property
    def _events(self) -> list[ClusterEvent]:
        result: list[ClusterEvent] = []
        seen: set[tuple[str, str, str]] = set()
        for index, row in enumerate(iter_tsv(self.root / _EVENTS)):
            try:
                envelope = json.loads(row.get("Body", ""))
            except ValueError:
                continue
            event = envelope.get("object", envelope) if isinstance(envelope, dict) else None
            if not isinstance(event, dict):
                continue
            involved = event.get("involvedObject")
            if not isinstance(involved, dict):
                continue
            ref = _entity(involved.get("kind"), involved.get("name"), involved.get("namespace"))
            if ref is None:
                continue
            metadata = child(event, "metadata")
            key = (
                str(metadata.get("uid") or metadata.get("name") or index),
                str(event.get("count")),
                str(event.get("lastTimestamp")),
            )
            if key in seen:
                continue
            seen.add(key)
            first = parse_time(event.get("firstTimestamp")) or parse_time(event.get("eventTime"))
            last = parse_time(event.get("lastTimestamp")) or first
            result.append(
                ClusterEvent(
                    entity=ref,
                    reason=str(event.get("reason") or ""),
                    type=str(event.get("type") or "Normal"),
                    message=str(event.get("message") or "")[:500],
                    first_at=first,
                    last_at=last,
                    count=int(event.get("count") or 1),
                    evidence_id=f"{_EVENTS}:{index}",
                )
            )
        return result

    def events(self) -> Sequence[ClusterEvent]:
        return self._events

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        """Error-level log lines for one service, bounded."""
        result: list[dict[str, Any]] = []
        for row in iter_tsv(self.root / _LOGS):
            if row.get("ServiceName") != service:
                continue
            severity = str(row.get("SeverityText") or "").upper()
            if severity not in {"ERROR", "FATAL", "CRITICAL", "WARN", "WARNING"}:
                continue
            result.append(
                {
                    "timestamp": row.get("Timestamp"),
                    "severity": severity,
                    "body": str(row.get("Body") or "")[:300],
                }
            )
            if len(result) >= limit:
                break
        return result


__all__ = ["SnapshotSource", "parse_time"]
