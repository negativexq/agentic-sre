"""Initial-observation views and targeted, read-only investigation access."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

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
from packages.rca.signals import extract_symptoms, symptom_entities
from packages.rca.source import ObservationSource
from packages.rca.topology import Topology, derive_edges


def _in_window(at: datetime | None, query: InvestigationQuery, cutoff: datetime | None) -> bool:
    if at is None:
        return True
    if query.start is not None and at < query.start:
        return False
    end = query.end or cutoff
    return end is None or at <= end


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

    def query_logs(self, target: EntityRef, query: InvestigationQuery) -> tuple[LogRecord, ...]: ...

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]: ...

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]: ...

    def supports(self, capability: str) -> bool: ...


@dataclass(frozen=True)
class SourceInvestigationBackend:
    """Adapter that exposes raw source records without exposing them to seed RCA."""

    source: ObservationSource

    def supports(self, capability: str) -> bool:
        methods = {
            "history": "object_history",
            "events": "events",
            "logs": "error_logs",
            "resource_pressure": "resource_pressure",
            "traffic": "traffic_observations",
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
            and _in_window(event.last_at or event.first_at, query, self.source.observation_cutoff())
            and (not query.reasons or event.reason in query.reasons)
            and (
                not query.contains
                or any(term.casefold() in event.message.casefold() for term in query.contains)
            )
        ]
        return tuple(events[: query.limit])

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

    if isinstance(source, InitialObservationView):
        return SourceInvestigationBackend(source.full_source)
    return SourceInvestigationBackend(source)


__all__ = [
    "InitialAccessLedger",
    "InitialObservationView",
    "InvestigationBackend",
    "SeedPolicy",
    "SourceInvestigationBackend",
    "initial_view",
    "investigation_backend",
]
