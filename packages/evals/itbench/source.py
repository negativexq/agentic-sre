"""Observation source over one ITBench-Lite snapshot. Never reads ground truth."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory, ITBenchScenario
from packages.evals.itbench.dataset import iter_tsv
from packages.rca.json_access import child
from packages.rca.model import (
    CLUSTER_SCOPE,
    Alert,
    ClusterEvent,
    EntityRef,
    Lifecycle,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    TrafficObservation,
)

_OBJECTS = "k8s_objects_raw.tsv"
_EVENTS = "k8s_events_raw.tsv"
_LOGS = "otel_logs_raw.tsv"
_ERROR_SEVERITIES = frozenset({"ERROR", "FATAL", "CRITICAL", "WARN", "WARNING"})
_MAX_ERRORS_PER_SERVICE = 200
_MEMORY_USAGE = "node_namespace_pod_container:container_memory_working_set_bytes"
_MEMORY_LIMIT = "cluster:namespace:pod_memory:active:kube_pod_container_resource_limits"
_CPU_THROTTLED = "container_cpu_cfs_throttled_periods_total"
_CPU_PERIODS = "container_cpu_cfs_periods_total"
_PRESSURE_METRICS = frozenset({_MEMORY_USAGE, _MEMORY_LIMIT, _CPU_THROTTLED, _CPU_PERIODS})
_MIN_CPU_PERIODS = 100
_TRAFFIC_METRIC = re.compile(r"(?:request|http|throughput|traffic|rps)", re.IGNORECASE)
_TRAFFIC_LABELS = ("service_name", "service", "workload", "app", "k8s_app")


def _tags(value: str) -> dict[str, Any]:
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


class _Split:
    """Before/after aggregates for one container resource."""

    def __init__(self) -> None:
        self.before: float | None = None
        self.after: float | None = None
        self.at: datetime | None = None
        self.index = 0


def pod_pressure(pod: EntityRef, path: Path, since: datetime) -> list[ResourcePressure]:
    """Memory-to-limit peaks and CPU throttling ratios per container, before and after ``since``."""
    memory: dict[str, _Split] = {}
    limits: dict[str, float] = {}
    # (container, metric, series) -> [first, last value before since, last value after]
    counters: dict[tuple[str, str, str], list[float | None]] = {}
    throttle_at: dict[str, datetime] = {}
    for index, row in enumerate(iter_tsv(path)):
        metric = row.get("metric_name")
        if metric not in _PRESSURE_METRICS:
            continue
        tags = _tags(str(row.get("tags") or ""))
        container = str(tags.get("container") or "")
        at = parse_time(row.get("timestamp"))
        if container in {"", "POD"} or at is None:
            continue
        try:
            value = float(row.get("value") or "nan")
        except ValueError:
            continue
        if value != value:  # NaN
            continue
        after = at >= since
        if metric == _MEMORY_LIMIT:
            limits[container] = max(limits.get(container, 0.0), value)
        elif metric == _MEMORY_USAGE:
            split = memory.setdefault(container, _Split())
            if not after:
                split.before = max(split.before or 0.0, value)
            elif value > (split.after or 0.0):
                split.after, split.at, split.index = value, at, index
        else:
            series = (container, metric, str(tags.get("id") or tags.get("name") or ""))
            bounds = counters.setdefault(series, [value, None, None])
            if after:
                bounds[2] = value
                if metric == _CPU_THROTTLED and container not in throttle_at:
                    throttle_at[container] = at
            else:
                bounds[1] = value
    relative = f"metrics/{path.name}"
    result: list[ResourcePressure] = []
    for container, split in sorted(memory.items()):
        limit = limits.get(container)
        if limit and split.after is not None:
            result.append(
                ResourcePressure(
                    pod=pod,
                    container=container,
                    resource="memory",
                    baseline=None if split.before is None else split.before / limit,
                    peak=split.after / limit,
                    at=split.at,
                    evidence_id=f"{relative}:{split.index}",
                )
            )
    earlier: dict[tuple[str, str], float] = {}
    recent: dict[tuple[str, str], float] = {}
    for (container, metric, _series), (first, middle, last) in counters.items():
        key = (container, metric)
        if middle is not None and first is not None:
            earlier[key] = earlier.get(key, 0.0) + middle - first
        if last is not None:
            start = middle if middle is not None else first
            recent[key] = recent.get(key, 0.0) + last - (start or 0.0)
    for container in sorted({key[0] for key in recent}):
        periods = recent.get((container, _CPU_PERIODS), 0.0)
        if periods < _MIN_CPU_PERIODS:
            continue
        base_periods = earlier.get((container, _CPU_PERIODS), 0.0)
        result.append(
            ResourcePressure(
                pod=pod,
                container=container,
                resource="cpu",
                baseline=(
                    earlier.get((container, _CPU_THROTTLED), 0.0) / base_periods
                    if base_periods >= _MIN_CPU_PERIODS
                    else None
                ),
                peak=recent.get((container, _CPU_THROTTLED), 0.0) / periods,
                at=throttle_at.get(container),
                evidence_id=f"{relative}:{_CPU_THROTTLED}",
            )
        )
    return result


def _mark_lifecycle(
    history: dict[EntityRef, list[ObjectVersion]],
) -> dict[EntityRef, list[ObjectVersion]]:
    """First versions are CREATED when the object was created after recording began."""
    firsts = [versions[0].observed_at for versions in history.values() if versions]
    if not firsts:
        return history
    recording_started = min(firsts)
    for versions in history.values():
        created = parse_time(child(versions[0].body, "metadata").get("creationTimestamp"))
        first = (
            Lifecycle.CREATED
            if created is not None and created > recording_started
            else Lifecycle.OBSERVED
        )
        versions[0] = versions[0].model_copy(update={"lifecycle": first})
    return history


class SnapshotSource:
    """Reads alerts, object versions, and events from snapshot files."""

    def __init__(self, scenario: ITBenchScenario) -> None:
        self.scenario = scenario
        self.root = Path(scenario.snapshot_path)

    def incident_id(self) -> str:
        return self.scenario.scenario_id

    @cached_property
    def _observation_cutoff(self) -> datetime | None:
        """Return snapshot metadata, or the latest observable record as fallback."""
        explicit = parse_time(self.scenario.observation_end)
        if explicit is not None:
            return explicit
        timestamps: list[datetime] = [
            version.observed_at for versions in self._history.values() for version in versions
        ]
        for item in self._events:
            event_at = item.last_at or item.first_at
            if event_at is not None:
                timestamps.append(event_at)
        timestamps.extend(item.at for item in self._error_logs if item.at is not None)
        timestamps.extend(item.starts_at for item in self._alerts)
        return max(timestamps) if timestamps else None

    def observation_cutoff(self) -> datetime | None:
        return self._observation_cutoff

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
        return _mark_lifecycle(history)

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

    @cached_property
    def _error_logs(self) -> list[LogRecord]:
        result: list[LogRecord] = []
        per_service: dict[str, int] = {}
        for index, row in enumerate(iter_tsv(self.root / _LOGS)):
            severity = str(row.get("SeverityText") or "").upper()
            number = (
                int(row.get("SeverityNumber") or 0)
                if str(row.get("SeverityNumber") or "").isdigit()
                else 0
            )
            if severity not in _ERROR_SEVERITIES and number < 13:
                continue
            service = str(row.get("ServiceName") or "")
            if not service or per_service.get(service, 0) >= _MAX_ERRORS_PER_SERVICE:
                continue
            per_service[service] = per_service.get(service, 0) + 1
            result.append(
                LogRecord(
                    service=service,
                    at=parse_time(row.get("Timestamp")),
                    severity=severity or str(number),
                    message=str(row.get("Body") or "")[:300],
                    evidence_id=f"{_LOGS}:{index}",
                )
            )
        return result

    def error_logs(self) -> Sequence[LogRecord]:
        return self._error_logs

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure]:
        result: list[ResourcePressure] = []
        for pod in pods:
            path = self.root / "metrics" / f"pod_{pod.name}_raw.tsv"
            if pod.kind == "Pod" and "/" not in pod.name and path.is_file():
                result.extend(pod_pressure(pod, path, since))
        return result

    @cached_property
    def _traffic_observations(self) -> list[TrafficObservation]:
        """Read only explicitly traffic-shaped metrics; steady-state values are not findings."""
        result: list[TrafficObservation] = []
        for path in sorted((self.root / "metrics").glob("*.tsv")):
            for index, row in enumerate(iter_tsv(path)):
                metric = str(row.get("metric_name") or "")
                if not _TRAFFIC_METRIC.search(metric):
                    continue
                at = parse_time(row.get("timestamp"))
                tags = _tags(str(row.get("tags") or ""))
                label = next((str(tags[key]) for key in _TRAFFIC_LABELS if tags.get(key)), "")
                if not label:
                    label = str(row.get("service_name") or "")
                if not label or at is None:
                    continue
                try:
                    value = float(row.get("value") or "nan")
                except ValueError:
                    continue
                if value != value:
                    continue
                result.append(
                    TrafficObservation(
                        entity=EntityRef(
                            kind="Service",
                            name=label,
                            namespace=str(row.get("namespace") or CLUSTER_SCOPE),
                        ),
                        metric=metric,
                        at=at,
                        value=value,
                        evidence_id=f"metrics/{path.name}:{index}",
                    )
                )
                if len(result) >= 5_000:
                    return result
        return result

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        cutoff = self.observation_cutoff()
        if cutoff is None:
            return self._traffic_observations
        return [item for item in self._traffic_observations if item.at <= cutoff]

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


__all__ = ["SnapshotSource", "parse_time", "pod_pressure"]
