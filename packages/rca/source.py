"""Read-only observation boundary used by the diagnosis engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    TrafficObservation,
)


class ObservationSource(Protocol):
    """Everything the engine may read about one incident.

    Implementations must be read-only. ``object_history`` returns every
    observed version per object, oldest first.
    """

    def incident_id(self) -> str: ...

    def observation_cutoff(self) -> datetime | None: ...

    def alerts(self) -> Sequence[Alert]: ...

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]: ...

    def events(self) -> Sequence[ClusterEvent]: ...

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]: ...

    def error_logs(self) -> Sequence[LogRecord]: ...

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure]:
        """Resource use of the given pods before and after ``since``; empty without metrics."""
        ...

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        """Bounded request-rate observations, empty when no metric source exists."""
        ...


@dataclass
class InMemorySource:
    """A fixed source for tests and demos."""

    name: str
    alert_items: list[Alert] = field(default_factory=list)
    versions: list[ObjectVersion] = field(default_factory=list)
    event_items: list[ClusterEvent] = field(default_factory=list)
    log_items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error_items: list[LogRecord] = field(default_factory=list)
    pressure_items: list[ResourcePressure] = field(default_factory=list)
    traffic_items: list[TrafficObservation] = field(default_factory=list)
    cutoff: datetime | None = None

    def incident_id(self) -> str:
        return self.name

    def observation_cutoff(self) -> datetime | None:
        return self.cutoff

    def alerts(self) -> Sequence[Alert]:
        return self.alert_items

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        history: dict[EntityRef, list[ObjectVersion]] = {}
        versions = [
            version
            for version in self.versions
            if self.cutoff is None or version.observed_at <= self.cutoff
        ]
        for version in sorted(versions, key=lambda item: item.observed_at):
            history.setdefault(version.entity, []).append(version)
        return history

    def events(self) -> Sequence[ClusterEvent]:
        if self.cutoff is None:
            return self.event_items
        return [
            event
            for event in self.event_items
            if (event.last_at or event.first_at or self.cutoff) <= self.cutoff
        ]

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        return self.log_items.get(service, [])[:limit]

    def error_logs(self) -> Sequence[LogRecord]:
        if self.cutoff is None:
            return self.error_items
        return [item for item in self.error_items if item.at is None or item.at <= self.cutoff]

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure]:
        wanted = set(pods)
        return [item for item in self.pressure_items if item.pod in wanted]

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        if self.cutoff is None:
            return self.traffic_items
        return [item for item in self.traffic_items if item.at <= self.cutoff]


__all__ = ["InMemorySource", "ObservationSource"]
