"""Initial-observation views and targeted, read-only investigation access."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from packages.rca.investigation.prometheus import PrometheusMetricsReader
from packages.rca.investigation.tempo import TempoTraceReader
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.signals import (
    _HPA_FAILURE_REASONS,
    _LIMIT_MESSAGE,
    _QUOTA_MESSAGE,
    extract_symptoms,
    symptom_entities,
)
from packages.rca.source import ObservationSource
from packages.rca.topology import Topology, derive_edges, is_chaos_kind

INCIDENT_CHANGE_DISCOVERY_KINDS = frozenset(
    {
        "ConfigMap",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "Job",
        "CronJob",
        "Service",
        "HorizontalPodAutoscaler",
        "HPA",
        "NetworkPolicy",
        "ResourceQuota",
        "LimitRange",
    }
)


def _in_window(at: datetime | None, query: InvestigationQuery, cutoff: datetime | None) -> bool:
    if at is None:
        return True
    if query.start is not None and at < query.start:
        return False
    end = query.end or cutoff
    return end is None or at <= end


def _event_time(event: ClusterEvent) -> datetime | None:
    return event.last_at or event.first_at


def _event_sort_key(event: ClusterEvent) -> tuple[str, str, str, str]:
    at = _event_time(event)
    return (
        at.isoformat() if at is not None else "",
        event.entity.canonical,
        event.reason,
        event.evidence_id,
    )


def _event_projection_group(event: ClusterEvent) -> tuple[int, int, str, str, str, str]:
    """Return the existing signal semantics used to compact one event group."""
    entity = event.entity.canonical
    if is_chaos_kind(event.entity.kind):
        reason_priority = {
            "Applied": 0,
            "Spawned": 0,
            "Failed": 1,
            "Started": 2,
            "Recovered": 3,
            "TimeUp": 4,
            "Paused": 5,
            "FinalizerInited": 6,
            "Updated": 7,
        }.get(event.reason, 8)
        return (0, reason_priority, "fault", entity, event.reason, event.type)
    if event.entity.kind in {"HorizontalPodAutoscaler", "HPA"} and (
        event.reason in _HPA_FAILURE_REASONS or event.type == "Warning"
    ):
        return (
            1,
            0 if event.reason in _HPA_FAILURE_REASONS else 1,
            "hpa",
            entity,
            event.reason,
            event.type,
        )
    if event.type == "Warning" and event.entity.kind not in {
        "PersistentVolume",
        "PersistentVolumeClaim",
        "VolumeAttachment",
    }:
        return (2, 0, "warning", entity, event.reason, event.type)
    if match := _QUOTA_MESSAGE.search(event.message):
        return (3, 0, "quota", entity, match.group(1) or "", event.type)
    if match := _LIMIT_MESSAGE.search(event.message):
        return (3, 0, "limit", entity, f"{match.group(1)}:{match.group(2)}", event.type)
    return (4, 0, "other", entity, event.reason, event.type)


def _project_incident_events(
    events: Sequence[ClusterEvent], limit: int
) -> tuple[ClusterEvent, ...]:
    """Keep bounded raw representatives for the signal-relevant event groups."""
    if limit <= 0:
        return ()
    grouped: dict[tuple[int, int, str, str, str, str], list[ClusterEvent]] = {}
    for event in sorted(events, key=_event_sort_key):
        grouped.setdefault(_event_projection_group(event), []).append(event)

    ordered_groups = sorted(grouped.items(), key=lambda item: item[0])
    selected: list[ClusterEvent] = []
    for _, items in ordered_groups:
        selected.append(items[0])
        if len(selected) == limit:
            break
    if len(selected) < limit:
        for _, items in ordered_groups:
            if len(items) > 1:
                selected.append(items[-1])
                if len(selected) == limit:
                    break
    return tuple(sorted(selected, key=_event_sort_key))


def _default_query(
    query: InvestigationQuery | None, *, onset: datetime | None
) -> InvestigationQuery:
    if query is not None:
        return query
    if onset is None:
        return InvestigationQuery()
    return InvestigationQuery(
        start=onset - timedelta(minutes=30),
        end=onset + timedelta(minutes=30),
        limit=32,
    )


class InvestigationBackend(Protocol):
    """Raw, bounded query surface used only after initial diagnosis."""

    def query_history(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]: ...

    def query_events(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]: ...

    def query_incident_events(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]: ...

    def query_incident_changes(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]: ...

    def query_logs(self, target: EntityRef, query: InvestigationQuery) -> tuple[LogRecord, ...]: ...

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]: ...

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]: ...

    def query_traces(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]: ...

    def supports(self, capability: str) -> bool: ...


@dataclass(frozen=True)
class SourceInvestigationBackend:
    """Adapter that exposes raw source records without exposing them to seed RCA."""

    source: ObservationSource

    def supports(self, capability: str) -> bool:
        source_supports = getattr(self.source, "supports", None)
        if callable(source_supports):
            aliases = {"incident_events": "events", "incident_changes": "history"}
            alias = aliases.get(capability)
            return bool(
                source_supports(capability) or (alias is not None and source_supports(alias))
            )
        methods = {
            "history": "object_history",
            "events": "events",
            "incident_events": "events",
            "incident_changes": "object_history",
            "logs": "error_logs",
            "resource_pressure": "resource_pressure",
            "traffic": "traffic_observations",
            "runtime_traces": "trace_observations",
        }
        method = methods.get(capability)
        return method is not None and callable(getattr(self.source, method, None))

    def query_history(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        versions = self.source.object_history().get(target, ())
        return tuple(
            version
            for version in versions
            if _in_window(version.observed_at, query, self.source.observation_cutoff())
        )[: query.limit]

    def query_events(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        events = [
            event
            for event in self.source.events()
            if event.entity == target
            and _in_window(_event_time(event), query, self.source.observation_cutoff())
            and (not query.reasons or event.reason in query.reasons)
            and (
                not query.contains
                or any(term.casefold() in event.message.casefold() for term in query.contains)
            )
        ]
        return tuple(events[: query.limit])

    def query_incident_events(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        if namespace.kind != "Namespace":
            raise ValueError("incident event discovery requires a Namespace target")
        events = [
            event
            for event in self.source.events()
            if event.entity.namespace == namespace.name
            and _in_window(event.last_at or event.first_at, query, self.source.observation_cutoff())
        ]
        return _project_incident_events(events, min(query.limit, 64))

    def query_incident_changes(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        if namespace.kind != "Namespace":
            raise ValueError("incident change discovery requires a Namespace target")
        grouped: list[tuple[datetime, str, tuple[ObjectVersion, ...]]] = []
        for entity, versions in self.source.object_history().items():
            if (
                entity.namespace != namespace.name
                or entity.kind not in INCIDENT_CHANGE_DISCOVERY_KINDS
            ):
                continue
            ordered = tuple(sorted(versions, key=lambda item: (item.observed_at, item.evidence_id)))
            in_window = tuple(
                version
                for version in ordered
                if _in_window(version.observed_at, query, self.source.observation_cutoff())
            )
            if not in_window:
                continue
            selected = list(in_window[:4])
            predecessor = None
            if query.start is not None:
                predecessor = next(
                    (version for version in reversed(ordered) if version.observed_at < query.start),
                    None,
                )
            if predecessor is not None and predecessor not in selected:
                selected.insert(0, predecessor)
            grouped.append((in_window[0].observed_at, entity.canonical, tuple(selected)))
        grouped.sort(key=lambda item: (item[0], item[1]))
        result = [version for _, _, versions in grouped for version in versions]
        result.sort(key=lambda item: (item.observed_at, item.entity.canonical, item.evidence_id))
        return tuple(result[: min(query.limit, 64)])

    def query_logs(self, target: EntityRef, query: InvestigationQuery) -> tuple[LogRecord, ...]:
        latest = {
            entity: versions[-1]
            for entity, versions in self.source.object_history().items()
            if versions
        }
        topology = Topology(derive_edges(latest, self.source.events()), latest)
        services = {target.name, target.canonical, *topology.service_names(target)}
        records = [
            item
            for item in self.source.error_logs()
            if item.service in services
            and _in_window(item.at, query, self.source.observation_cutoff())
            and (
                not query.contains
                or any(term.casefold() in item.message.casefold() for term in query.contains)
            )
        ]
        return tuple(records[: query.limit])

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        since = query.start or datetime.min.replace(tzinfo=UTC)
        records = self.source.resource_pressure((target,), since)
        return tuple(
            item for item in records if _in_window(item.at, query, self.source.observation_cutoff())
        )[: query.limit]

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        records = [
            item
            for item in self.source.traffic_observations()
            if item.entity == target
            and _in_window(item.at, query, self.source.observation_cutoff())
        ]
        return tuple(records[: query.limit])

    def query_traces(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]:
        return _select_runtime_trace_context(
            self.source.trace_observations(),
            target,
            query,
            self.source.observation_cutoff(),
        )


def _trace_matches_target(span: TraceSpanObservation, target: EntityRef) -> bool:
    """Match only exact Kubernetes semantic attributes for a trace target."""
    namespace = span.semantic_attributes.get("k8s.namespace.name")
    if namespace != target.namespace:
        return False
    attribute_by_kind = {
        "Pod": "k8s.pod.name",
        "Deployment": "k8s.deployment.name",
        "StatefulSet": "k8s.statefulset.name",
        "DaemonSet": "k8s.daemonset.name",
    }
    attribute = attribute_by_kind.get(target.kind)
    return attribute is not None and span.semantic_attributes.get(attribute) == target.name


def _trace_in_window(
    span: TraceSpanObservation, query: InvestigationQuery, cutoff: datetime | None
) -> bool:
    if query.start is not None and span.start_at < query.start:
        return False
    if query.end is not None and span.start_at > query.end:
        return False
    return cutoff is None or span.start_at <= cutoff


def _select_runtime_trace_context(
    spans: Sequence[TraceSpanObservation],
    target: EntityRef,
    query: InvestigationQuery,
    cutoff: datetime | None,
) -> tuple[TraceSpanObservation, ...]:
    if query.reasons or query.contains:
        raise ValueError("runtime_traces does not accept reasons/contains filters")
    if query.limit > 32:
        raise ValueError("runtime_traces limit must be at most 32")
    eligible = [span for span in spans if _trace_in_window(span, query, cutoff)]
    seeds = sorted(
        (span for span in eligible if _trace_matches_target(span, target)),
        key=lambda span: (span.start_at, span.trace_id, span.span_id, span.evidence_id),
    )
    by_span = {(span.trace_id, span.span_id): span for span in eligible}
    children_by_parent: dict[tuple[str, str], list[TraceSpanObservation]] = {}
    for span in eligible:
        if span.parent_span_id is not None:
            children_by_parent.setdefault((span.trace_id, span.parent_span_id), []).append(span)
    selected: dict[str, TraceSpanObservation] = {}
    for seed in seeds:
        selected[seed.evidence_id] = seed
        if seed.parent_span_id is not None:
            parent = by_span.get((seed.trace_id, seed.parent_span_id))
            if parent is not None:
                selected[parent.evidence_id] = parent
        for child in children_by_parent.get((seed.trace_id, seed.span_id), ()):
            selected[child.evidence_id] = child
    ordered = sorted(
        selected.values(),
        key=lambda span: (span.start_at, span.trace_id, span.span_id, span.evidence_id),
    )
    return tuple(ordered[: query.limit])


def _query_trace_observations(
    spans: Sequence[TraceSpanObservation],
    target: EntityRef,
    query: InvestigationQuery,
    cutoff: datetime | None,
) -> tuple[TraceSpanObservation, ...]:
    """Compatibility name for the A2 source-backed selector."""
    return _select_runtime_trace_context(spans, target, query, cutoff)


@dataclass(frozen=True)
class TempoInvestigationBackend:
    """Existing investigation reads plus an active Tempo trace provider."""

    base: InvestigationBackend
    tempo: TempoTraceReader
    observation_cutoff: datetime | None

    def query_history(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        return self.base.query_history(target, query)

    def query_events(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        return self.base.query_events(target, query)

    def query_incident_events(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        return self.base.query_incident_events(namespace, query)

    def query_incident_changes(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        return self.base.query_incident_changes(namespace, query)

    def query_logs(self, target: EntityRef, query: InvestigationQuery) -> tuple[LogRecord, ...]:
        return self.base.query_logs(target, query)

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        return self.base.query_resource_pressure(target, query)

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        return self.base.query_traffic(target, query)

    def query_traces(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]:
        batch = self.tempo.query(target, query)
        return _select_runtime_trace_context(batch.spans, target, query, self.observation_cutoff)

    def supports(self, capability: str) -> bool:
        if capability == "runtime_traces":
            return True
        return self.base.supports(capability)


@dataclass(frozen=True)
class PrometheusInvestigationBackend:
    """Existing investigation reads plus active Prometheus metric reads."""

    base: InvestigationBackend
    prometheus: PrometheusMetricsReader
    observation_cutoff: datetime | None

    def _effective_query(self, query: InvestigationQuery) -> InvestigationQuery | None:
        if self.observation_cutoff is None:
            return query
        if query.start is not None and query.start > self.observation_cutoff:
            return None
        if query.end is None or query.end > self.observation_cutoff:
            return query.model_copy(update={"end": self.observation_cutoff})
        return query

    def query_history(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        return self.base.query_history(target, query)

    def query_events(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        return self.base.query_events(target, query)

    def query_incident_events(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ClusterEvent, ...]:
        return self.base.query_incident_events(namespace, query)

    def query_incident_changes(
        self, namespace: EntityRef, query: InvestigationQuery
    ) -> tuple[ObjectVersion, ...]:
        return self.base.query_incident_changes(namespace, query)

    def query_logs(self, target: EntityRef, query: InvestigationQuery) -> tuple[LogRecord, ...]:
        return self.base.query_logs(target, query)

    def query_traces(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]:
        return self.base.query_traces(target, query)

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        effective = self._effective_query(query)
        if effective is None:
            return ()
        return self.prometheus.query_resource_pressure(target, effective)

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        effective = self._effective_query(query)
        if effective is None:
            return ()
        return self.prometheus.query_traffic(target, effective)

    def supports(self, capability: str) -> bool:
        if capability in {"resource_pressure", "traffic"}:
            return True
        return self.base.supports(capability)


@dataclass(frozen=True)
class SeedPolicy:
    """Generic initial-access contract, independent of incident labels."""

    name: str = "latest-only-plus-5m-events"
    history_window: timedelta | None = None
    event_before: timedelta | None = timedelta(minutes=5)
    event_after: timedelta | None = timedelta(minutes=5)


@dataclass
class InitialAccessLedger:
    """Developer-visible raw references consumed by one bounded seed pass."""

    history_refs: set[str] = field(default_factory=set)
    event_refs: set[str] = field(default_factory=set)
    log_refs: set[str] = field(default_factory=set)
    metric_refs: set[str] = field(default_factory=set)
    traffic_refs: set[str] = field(default_factory=set)
    trace_refs: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, tuple[str, ...]]:
        return {
            "initial_history_refs": tuple(sorted(self.history_refs)),
            "initial_event_refs": tuple(sorted(self.event_refs)),
            "initial_log_refs": tuple(sorted(self.log_refs)),
            "initial_metric_refs": tuple(sorted(self.metric_refs)),
            "initial_traffic_refs": tuple(sorted(self.traffic_refs)),
            "initial_trace_refs": tuple(sorted(self.trace_refs)),
        }


@dataclass(frozen=True)
class InitialObservationView:
    """Generic bounded seed view over a full incident source.

    Latest object state and alert-local topology are cheap seed observations.
    Historical versions, wider event windows, logs, and metric series remain in
    the investigation backend until a targeted query requests them.
    """

    full_source: ObservationSource
    seed_policy: SeedPolicy = field(default_factory=SeedPolicy)

    # Information-gap derivation uses this marker to distinguish a bounded
    # initial view from a full deterministic source.  It is metadata about the
    # access contract, not evidence about the incident.
    initial_observation_bounded: bool = True
    access: InitialAccessLedger = field(default_factory=InitialAccessLedger, compare=False)

    def _latest(self) -> dict[EntityRef, ObjectVersion]:
        return {
            entity: versions[-1]
            for entity, versions in self.full_source.object_history().items()
            if versions
        }

    def _seed_entities(self) -> set[EntityRef]:
        latest = self._latest()
        # Event-derived chaos edges are investigation telemetry.  Including
        # the full event journal here would let hidden Schedule/Chaos events
        # alter the initial topology before a targeted query is requested.
        topology = Topology(derive_edges(latest), latest)
        entities = symptom_entities(self.full_source.alerts(), topology)
        seeded = set(entities)
        for entity in entities:
            # Keep the alert-local causal neighborhood, including one upstream
            # dependency/configuration hop beyond the workload.  This exposes
            # enough structure to form useful hypotheses while historical
            # records, metrics, and wider events remain query-only.
            seeded.update(topology.reachable(entity, max_depth=2))
        return seeded

    def incident_id(self) -> str:
        return self.full_source.incident_id()

    def observation_cutoff(self) -> datetime | None:
        return self.full_source.observation_cutoff()

    def alerts(self) -> tuple[Alert, ...]:
        return tuple(self.full_source.alerts())

    def object_history(self) -> dict[EntityRef, tuple[ObjectVersion, ...]]:
        full = self.full_source.object_history()
        seeded = self._seed_entities()
        onset = extract_symptoms(self.full_source.alerts()).onset
        start = (
            onset - self.seed_policy.history_window
            if onset is not None and self.seed_policy.history_window is not None
            else None
        )
        result: dict[EntityRef, tuple[ObjectVersion, ...]] = {}
        for entity, versions in full.items():
            if not versions:
                continue
            if entity not in seeded or start is None:
                result[entity] = (versions[-1],)
                continue
            visible = tuple(version for version in versions if version.observed_at >= start)
            result[entity] = visible or (versions[-1],)
        self.access.history_refs.update(
            version.evidence_id for versions in result.values() for version in versions
        )
        return result

    def events(self) -> tuple[ClusterEvent, ...]:
        onset = extract_symptoms(self.full_source.alerts()).onset
        if (
            onset is None
            or self.seed_policy.event_before is None
            or self.seed_policy.event_after is None
        ):
            return ()
        start = onset - self.seed_policy.event_before
        end = onset + self.seed_policy.event_after
        seeded = self._seed_entities()
        visible = tuple(
            event
            for event in self.full_source.events()
            if event.entity in seeded and start <= (event.last_at or event.first_at or onset) <= end
        )
        self.access.event_refs.update(event.evidence_id for event in visible)
        return visible

    def logs(self, service: str, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        onset = extract_symptoms(self.full_source.alerts()).onset
        if onset is None:
            return ()
        start = onset - (self.seed_policy.event_before or timedelta())
        end = onset + (self.seed_policy.event_after or timedelta())
        records = []
        for item in self.full_source.logs(service, limit=limit):
            raw_at = item.get("timestamp")
            if not isinstance(raw_at, str):
                continue
            try:
                at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= at <= end:
                records.append(item)
        return tuple(records[:limit])

    def access_ledger(self) -> dict[str, tuple[str, ...]]:
        """Return the bounded raw-access inventory for developer diagnostics."""
        return self.access.as_dict()

    def error_logs(self) -> tuple[LogRecord, ...]:
        # Logs are an investigation capability, not part of the bounded seed.
        # Returning the full alert-service history here would make
        # ``build_case(initial_view(source))`` consume the same evidence that a
        # later LogsTool query is supposed to acquire.
        return ()

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> tuple[ResourcePressure, ...]:
        del pods, since
        return ()

    def traffic_observations(self) -> tuple[TrafficObservation, ...]:
        return ()

    def trace_observations(self) -> tuple[TraceSpanObservation, ...]:
        return ()


def initial_view(
    source: ObservationSource, *, policy: SeedPolicy | None = None
) -> ObservationSource:
    """Return the bounded initial view for active investigation runs."""

    if isinstance(source, InitialObservationView):
        return source
    return InitialObservationView(source, seed_policy=policy or SeedPolicy())


def investigation_backend(source: ObservationSource) -> InvestigationBackend:
    """Resolve the full raw backend for a source or an initial view."""
    full_source = source.full_source if isinstance(source, InitialObservationView) else source
    custom = getattr(full_source, "investigation_backend", None)
    if callable(custom):
        candidate = custom()
        required = (
            "query_history",
            "query_events",
            "query_incident_events",
            "query_incident_changes",
            "query_logs",
            "query_resource_pressure",
            "query_traffic",
            "query_traces",
            "supports",
        )
        if all(callable(getattr(candidate, name, None)) for name in required):
            return cast(InvestigationBackend, candidate)
    return SourceInvestigationBackend(full_source)


__all__ = [
    "INCIDENT_CHANGE_DISCOVERY_KINDS",
    "InitialAccessLedger",
    "InitialObservationView",
    "InvestigationBackend",
    "PrometheusInvestigationBackend",
    "SeedPolicy",
    "SourceInvestigationBackend",
    "TempoInvestigationBackend",
    "initial_view",
    "investigation_backend",
    "_select_runtime_trace_context",
]
