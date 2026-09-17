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
)


class ObservationSource(Protocol):
    """Everything the engine may read about one incident.

    Implementations must be read-only. ``object_history`` returns every
    observed version per object, oldest first.
    """

    def incident_id(self) -> str: ...

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

    def incident_id(self) -> str:
        return self.name

    def alerts(self) -> Sequence[Alert]:
        return self.alert_items

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        history: dict[EntityRef, list[ObjectVersion]] = {}
        for version in sorted(self.versions, key=lambda item: item.observed_at):
            history.setdefault(version.entity, []).append(version)
        return history

    def events(self) -> Sequence[ClusterEvent]:
        return self.event_items

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        return self.log_items.get(service, [])[:limit]

    def error_logs(self) -> Sequence[LogRecord]:
        return self.error_items

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure]:
        wanted = set(pods)
        return [item for item in self.pressure_items if item.pod in wanted]


__all__ = ["InMemorySource", "ObservationSource"]
