"""The control-plane half of the boundary: the existing reader protocols, served over the wire.

``ConnectorClusterReader`` and the provider readers implement ``ClusterReader`` and the
Prometheus/Loki/Tempo reader protocols, so nothing above them changes. Every call is encoded,
sent through a ``Transport`` and decoded. A transport loss or a backend failure is raised, never
returned as an empty result, so callers that already map exceptions to failed reads keep doing so.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.connector import wire
from packages.connector.service import Connector
from packages.rca.investigation.tempo import TempoTraceBatch
from packages.rca.live import ListingFailure, ListingScope, ObjectListing
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.provider_adapter import ProviderReaders

Transport = Callable[[bytes], bytes]


class ConnectorError(RuntimeError):
    """A connector operation did not complete."""


class ConnectorUnavailable(ConnectorError):
    """The transport failed or returned something that is not a valid response."""


class ConnectorReadError(ConnectorError):
    """The connector reported that the backend read failed."""

    def __init__(self, remote_type: str, message: str) -> None:
        super().__init__(f"{remote_type}: {message}")
        self.remote_type = remote_type


def in_process_transport(connector: Connector) -> Transport:
    """Bytes in, bytes out: the same contract a network transport will honour."""
    return connector.handle


@dataclass(frozen=True)
class Page:
    """One page of a stream: items and gaps in sequence order, and where to resume."""

    epoch: str
    next_cursor: str
    items: tuple[wire.StreamItem, ...]
    gaps: tuple[wire.GapItem, ...]

    def ordered(self) -> list[wire.StreamItem]:
        """Gaps first (synthetic ones carry ``seq`` 0), then everything by sequence."""
        both: list[wire.StreamItem] = [*self.gaps, *self.items]
        return sorted(both, key=lambda item: item.seq)

    def exhausted(self, limit: int) -> bool:
        return len(self.items) + len(self.gaps) < limit


class ConnectorClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        request = wire.Request.model_validate(
            {"version": wire.WIRE_VERSION, "op": op, "args": args or {}}
        )
        try:
            response = wire.loads_response(self._transport(wire.dumps(request)))
        except Exception as error:
            raise ConnectorUnavailable(f"{type(error).__name__}: {error}") from error
        if response.version != wire.WIRE_VERSION:
            raise ConnectorUnavailable(f"unsupported wire version {response.version}")
        if not response.ok:
            if response.failure is None:
                raise ConnectorUnavailable("failure response without a failure")
            raise ConnectorReadError(response.failure.error_type, response.failure.message)
        if response.result is None:
            raise ConnectorUnavailable("success response without a result")
        return response.result

    def read(self, op: str, cursor: str | None, limit: int = wire.MAX_BATCH) -> Page:
        result = self.call(op, {"cursor": cursor, "limit": limit})
        try:
            return Page(
                epoch=str(result["epoch"]),
                next_cursor=str(result["next_cursor"]),
                items=tuple(wire.item_from_wire(i) for i in result["items"]),
                gaps=tuple(
                    g
                    for g in (wire.item_from_wire(i) for i in result["gaps"])
                    if isinstance(g, wire.GapItem)
                ),
            )
        except (KeyError, ValueError) as error:
            raise ConnectorUnavailable(f"malformed page: {error}") from error

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.call("capabilities")["capabilities"])

    def preflight(self) -> list[dict[str, Any]]:
        return list(self.call("preflight")["backends"])


class ConnectorClusterReader:
    def __init__(self, client: ConnectorClient) -> None:
        self._client = client

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        return wire.listing_from_wire(
            self._client.call("list_objects", {"namespaces": list(namespaces)})
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        return list(self._client.call("list_events", {"namespaces": list(namespaces)})["events"])


def _query_args(target: EntityRef, query: InvestigationQuery) -> dict[str, Any]:
    return {
        "target": target.model_dump(mode="json"),
        "query": query.model_dump(mode="json"),
    }


class ConnectorPrometheusReader:
    def __init__(self, client: ConnectorClient) -> None:
        self._client = client

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        result = self._client.call("query_resource_pressure", _query_args(target, query))
        return tuple(wire.records_from_wire("pressure", result))

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        result = self._client.call("query_traffic", _query_args(target, query))
        return tuple(wire.records_from_wire("traffic", result))


class ConnectorLokiReader:
    def __init__(self, client: ConnectorClient) -> None:
        self._client = client

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        result = self._client.call(
            "query_logs",
            {
                "services": list(services),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "limit": limit,
            },
        )
        return wire.records_from_wire("logs", result)


class ConnectorTempoReader:
    def __init__(self, client: ConnectorClient) -> None:
        self._client = client

    def query(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
        return wire.traces_from_wire(self._client.call("query_traces", _query_args(target, query)))


def provider_readers(client: ConnectorClient) -> ProviderReaders:
    """Readers for exactly the capabilities the connector reports (``supports`` is unchanged)."""
    capabilities = set(client.capabilities())
    return ProviderReaders(
        prometheus=(
            ConnectorPrometheusReader(client)
            if {"resource_pressure", "traffic"} & capabilities
            else None
        ),
        loki=ConnectorLokiReader(client) if "logs" in capabilities else None,
        tempo=ConnectorTempoReader(client) if "runtime_traces" in capabilities else None,
    )


def cluster_reader(client: ConnectorClient) -> ConnectorClusterReader | None:
    return ConnectorClusterReader(client) if "events" in client.capabilities() else None


class StreamedClusterReader:
    """A ``ClusterReader`` rebuilt from the ``changes`` stream (contract §10).

    The mirror is Control Plane memory: a fresh reader starts from a snapshot, applies deltas, and
    answers ``list_objects`` with exactly the completeness the Connector last declared. Nothing is
    served before a complete snapshot has arrived, and after a backend gap the listing raises
    instead of returning what it last knew.
    """

    def __init__(self, client: ConnectorClient, *, page_limit: int = wire.MAX_BATCH) -> None:
        self._client = client
        self._limit = page_limit
        self._cursor: str | None = None
        self._objects: dict[str, dict[str, Any]] = {}
        self._staging: dict[str, dict[str, Any]] | None = None
        self._events: dict[str, dict[str, Any]] = {}
        self._completed: frozenset[ListingScope] = frozenset()
        self._failed: tuple[ListingFailure, ...] = ()
        self._ready = False
        self._blocked: str | None = None
        self._lock = threading.RLock()
        self._applied = 0  # stream items applied so far
        # when the Connector observed each object and Event (contract §15, measurement only)
        self._observed: dict[str, datetime] = {}
        self._reported = 0  # ... as of the last ``pending`` call

    def pending(self) -> bool:
        """Drain the stream; whether anything arrived since the previous call (contract §15).

        Lets the control plane journal a change as soon as it arrives instead of on a fixed interval.
        An unreachable connector is not a change.
        """
        with self._lock:
            try:
                self._drain()
            except ConnectorError:
                return False
            changed = self._applied != self._reported
            self._reported = self._applied
            return changed

    def observed_at_of(self, body: dict[str, Any]) -> datetime | None:
        """When the Connector observed this object or Event, if it arrived through the stream."""
        with self._lock:
            key = _event_id(body) if body.get("kind") == "Event" else _key(body)
            return self._observed.get(key) if key is not None else None

    def _apply(self, item: wire.StreamItem) -> None:
        self._applied += 1
        if isinstance(item, wire.ObjectItem):
            key = _key(item.body)
            if key is not None:
                self._observed[key] = item.observed_at
        elif isinstance(item, wire.EventItem):
            self._observed[_event_id(item.body)] = item.observed_at
        if isinstance(item, wire.GapItem):
            if item.reason == "BACKEND_UNREACHABLE":
                self._blocked = f"the connector could not read the cluster at {item.at.isoformat()}"
            else:
                self._ready = False  # the next snapshot rebuilds the mirror
            return
        if isinstance(item, wire.SnapshotBeginItem):
            self._staging = {}
        elif isinstance(item, wire.ObjectItem):
            key = _key(item.body)
            if key is not None:
                (self._staging if self._staging is not None else self._objects)[key] = item.body
        elif isinstance(item, wire.ObjectDeletedItem):
            self._objects.pop(item.key, None)
        elif isinstance(item, wire.EventItem):
            self._events[_event_id(item.body)] = item.body
        elif isinstance(item, wire.ListingStatusItem):
            if item.snapshot:
                if self._staging is None:
                    return  # an end without its begin proves nothing
                self._objects, self._staging = self._staging, None
            elif not self._ready:
                return  # deltas before a complete snapshot describe nothing we hold
            self._completed = frozenset(
                ListingScope(s.namespace, s.kind) for s in item.completed_scopes
            )
            self._failed = tuple(
                ListingFailure(ListingScope(f.scope.namespace, f.scope.kind), f.error)
                for f in item.failed_scopes
            )
            self._ready, self._blocked = True, None

    def _drain(self) -> None:
        while True:
            page = self._client.read("read_changes", self._cursor, self._limit)
            for item in page.ordered():
                self._apply(item)
            self._cursor = page.next_cursor
            if page.exhausted(self._limit):
                return

    def _current(self) -> None:
        self._drain()
        if self._blocked is not None:
            raise ConnectorUnavailable(self._blocked)
        if not self._ready or self._staging is not None:
            raise ConnectorUnavailable("no complete cluster snapshot has been received")

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        with self._lock:
            self._current()
            wanted = set(namespaces)
            keys = sorted(k for k in self._objects if k.split("/", 1)[0] in wanted)
            return ObjectListing(
                tuple(self._objects[k] for k in keys),
                frozenset(s for s in self._completed if s.namespace in wanted),
                tuple(f for f in self._failed if f.scope.namespace in wanted),
            )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        with self._lock:
            self._current()
            wanted = set(namespaces)
            return [
                self._events[k]
                for k in sorted(self._events)
                if _namespace(self._events[k]) in wanted
            ]


def _key(body: dict[str, Any]) -> str | None:
    from packages.rca.model import object_key

    return object_key(body)


def _event_id(body: dict[str, Any]) -> str:
    metadata = body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("uid") or f"{metadata.get('namespace')}/{metadata.get('name')}")


def _namespace(body: dict[str, Any]) -> str | None:
    metadata = body.get("metadata")
    return metadata.get("namespace") if isinstance(metadata, dict) else None
