"""``connector.v1`` wire schema (docs/architecture/connector-boundary-contract.md §4).

Strict and versioned. Every request and response is canonical JSON bytes, so the in-process
transport exercises the same encoding a network transport will; nothing crosses the boundary as a
Python object.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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

WIRE_VERSION = "connector.v2"  # v2: watch-driven change stream (contract §15)
MAX_BATCH = 500
MAX_RESPONSE_BYTES = 16_000_000
MAX_NAMESPACES = 32
MAX_SERVICES = 64
# Kinds the Connector never returns, whatever the backend reader listed (contract §3, §8.2).
DENIED_KINDS = frozenset({"Secret"})

Op = Literal[
    "capabilities",
    "preflight",
    "list_objects",
    "list_events",
    "query_logs",
    "query_resource_pressure",
    "query_traffic",
    "query_traces",
    "read_alerts",
    "read_changes",
]


class WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Request(WireModel):
    version: str
    op: Op
    args: dict[str, Any] = Field(default_factory=dict)


class Failure(WireModel):
    """A read that did not complete. Never an empty result (contract §5.1)."""

    error_type: str
    message: str


class Response(WireModel):
    version: str
    ok: bool
    result: dict[str, Any] | None = None
    failure: Failure | None = None


class NamespaceArgs(WireModel):
    namespaces: list[str] = Field(max_length=MAX_NAMESPACES)


class TargetQueryArgs(WireModel):
    target: EntityRef
    query: InvestigationQuery


class LogArgs(WireModel):
    services: list[str] = Field(max_length=MAX_SERVICES)
    starts_at: datetime
    ends_at: datetime
    limit: int | None = Field(default=None, ge=1, le=64)


class ScopeWire(WireModel):
    namespace: str
    kind: str


class ScopeFailureWire(WireModel):
    scope: ScopeWire
    error: str


class ObjectListingWire(WireModel):
    objects: list[dict[str, Any]]
    completed_scopes: list[ScopeWire]
    failed_scopes: list[ScopeFailureWire]


_TEMPO = TypeAdapter(TempoTraceBatch)
_RECORDS: dict[str, TypeAdapter[Any]] = {
    "logs": TypeAdapter(list[LogRecord]),
    "pressure": TypeAdapter(list[ResourcePressure]),
    "traffic": TypeAdapter(list[TrafficObservation]),
    "spans": TypeAdapter(list[TraceSpanObservation]),
}


def dumps(model: BaseModel) -> bytes:
    """Canonical bytes: sorted keys, no whitespace; NaN, Infinity and non-JSON values raise.

    The wire models hold only JSON primitives (records are encoded before they get here), so
    python-mode dumping keeps a NaN visible for ``json.dumps`` to refuse instead of letting
    pydantic's JSON mode turn it into ``null``.
    """
    return json.dumps(
        model.model_dump(mode="python"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def loads_request(payload: bytes) -> Request:
    return Request.model_validate_json(payload)


def loads_response(payload: bytes) -> Response:
    return Response.model_validate_json(payload)


def listing_to_wire(listing: ObjectListing) -> dict[str, Any]:
    wire = ObjectListingWire(
        objects=list(listing.objects),
        completed_scopes=[
            ScopeWire(namespace=s.namespace, kind=s.kind)
            for s in sorted(listing.completed_scopes, key=lambda s: (s.namespace, s.kind))
        ],
        failed_scopes=[
            ScopeFailureWire(
                scope=ScopeWire(namespace=f.scope.namespace, kind=f.scope.kind), error=f.error
            )
            for f in listing.failed_scopes
        ],
    )
    return wire.model_dump(mode="python")


def listing_from_wire(result: dict[str, Any]) -> ObjectListing:
    wire = ObjectListingWire.model_validate(result)
    return ObjectListing(
        tuple(wire.objects),
        frozenset(ListingScope(s.namespace, s.kind) for s in wire.completed_scopes),
        tuple(
            ListingFailure(ListingScope(f.scope.namespace, f.scope.kind), f.error)
            for f in wire.failed_scopes
        ),
    )


def _faithful(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Encode ``value`` and refuse any encoding that does not decode back to the same value.

    JSON mode silently turns NaN into null; an observation must never change on the way through.
    """
    encoded = adapter.dump_python(value, mode="json")
    try:
        faithful = adapter.validate_python(encoded) == value
    except ValueError:
        faithful = False
    if not faithful:
        raise ValueError("observation is not faithfully representable on the wire")
    return encoded


def records_to_wire(kind: str, records: Any) -> dict[str, Any]:
    return {"records": _faithful(_RECORDS[kind], list(records))}


def records_from_wire(kind: str, result: dict[str, Any]) -> list[Any]:
    return list(_RECORDS[kind].validate_python(result["records"]))


def traces_to_wire(value: Any) -> dict[str, Any]:
    if isinstance(value, TempoTraceBatch):
        return {"shape": "tempo_batch", "batch": _faithful(_TEMPO, value)}
    return {"shape": "spans", **records_to_wire("spans", value)}


def traces_from_wire(result: dict[str, Any]) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
    if result["shape"] == "tempo_batch":
        return _TEMPO.validate_python(result["batch"])
    return tuple(records_from_wire("spans", result))


# ---- streams as cursor-paged reads (contract §10) -------------------------------------------

GapReason = Literal[
    "CONNECTOR_RESTART", "BUFFER_EXPIRED", "BACKEND_UNREACHABLE", "RESOURCE_VERSION_EXPIRED"
]


class ReadArgs(WireModel):
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_BATCH)


class StreamItem(WireModel):
    """Common shape of everything a stream carries: a tag and a sequence number."""

    kind: str
    seq: int


class AlertItem(StreamItem):
    """One alert occurrence in Alertmanager's own payload shape, so intake stays identical."""

    kind: Literal["alert"] = "alert"
    observed_at: datetime
    origin: Literal["webhook", "poll"]
    alert: dict[str, Any]


class HeartbeatItem(StreamItem):
    """One poll of the alert channel as the Connector made it (feeds coverage segments)."""

    kind: Literal["heartbeat"] = "heartbeat"
    attempted_at: datetime
    completed_at: datetime
    ok: bool
    active_alerts: int | None = None
    error_type: str | None = None


class ObjectItem(StreamItem):
    kind: Literal["object"] = "object"
    observed_at: datetime
    body: dict[str, Any]


class ObjectDeletedItem(StreamItem):
    kind: Literal["object_deleted"] = "object_deleted"
    observed_at: datetime
    key: str
    # contract §15: "watch" is an observed deletion on a continuous watch; "listing" an inferred one
    source: Literal["watch", "listing"] = "listing"


class EventItem(StreamItem):
    kind: Literal["event"] = "event"
    observed_at: datetime
    body: dict[str, Any]


class SnapshotBeginItem(StreamItem):
    kind: Literal["snapshot_begin"] = "snapshot_begin"
    observed_at: datetime
    # contract §15.4: set for one scope's relist; absent for a global snapshot
    scope: ScopeWire | None = None


class ListingStatusItem(StreamItem):
    """Scope completeness of the listing behind the preceding items.

    With ``snapshot`` true it is the ``end`` of a snapshot: a snapshot without it proves nothing.
    """

    kind: Literal["listing_status"] = "listing_status"
    observed_at: datetime
    snapshot: bool
    completed_scopes: list[ScopeWire]
    failed_scopes: list[ScopeFailureWire]
    # contract §15.4: the end of one scope's relist
    scope: ScopeWire | None = None


class GapItem(StreamItem):
    kind: Literal["gap"] = "gap"
    at: datetime
    reason: GapReason
    # contract §15.4: the one scope whose continuity was lost; absent for a global gap
    scope: ScopeWire | None = None


STREAM_ITEMS: dict[str, type[StreamItem]] = {
    "alert": AlertItem,
    "heartbeat": HeartbeatItem,
    "object": ObjectItem,
    "object_deleted": ObjectDeletedItem,
    "event": EventItem,
    "snapshot_begin": SnapshotBeginItem,
    "listing_status": ListingStatusItem,
    "gap": GapItem,
}


def item_from_wire(value: dict[str, Any]) -> StreamItem:
    kind = value.get("kind")
    model = STREAM_ITEMS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ValueError(f"unknown stream item kind {kind!r}")
    return model.model_validate(value)


def make_cursor(epoch: str, seq: int) -> str:
    return f"{epoch}:{seq}"


def parse_cursor(cursor: str) -> tuple[str, int]:
    epoch, _, seq = cursor.rpartition(":")
    if not epoch or not seq.isdigit():
        raise ValueError(f"malformed cursor {cursor!r}")
    return epoch, int(seq)
