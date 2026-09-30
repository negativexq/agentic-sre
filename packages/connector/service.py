"""The customer-side half of the boundary: typed, bounded, audited reads (contract §3, §5).

The Connector owns the backend readers and every limit. It accepts only the operations of
``connector.v1``; there is no query pass-through and no write. A failed read is a ``Failure``
response, never an empty result.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from packages.connector import wire
from packages.connector.streams import StreamBuffer
from packages.contracts import AlertmanagerWebhook
from packages.rca.alert_coverage import (
    AlertmanagerConfig,
    AlertmanagerReader,
    fetch_alerts,
)
from packages.rca.live import (
    ClusterReader,
    KubernetesClusterReader,
    ListingScope,
    ObjectListing,
)
from packages.rca.model import object_key
from packages.rca.provider_adapter import ProviderReaders

logger = logging.getLogger(__name__)

CONNECTOR_VERSION = "0.1"
_MESSAGE_LIMIT = 500


class InvalidArguments(ValueError):
    """The request was well-formed but its arguments broke the operation's schema."""


def _args[M: BaseModel](model: type[M], args: dict[str, Any]) -> M:
    try:
        return model.model_validate(args)
    except ValidationError as error:
        raise InvalidArguments(str(error)) from error


class AlertSource(Protocol):
    """What the Connector needs from Alertmanager: the alerts it reports now, or raise."""

    def list_alerts(self) -> list[dict[str, Any]]: ...


def _digest(body: dict[str, Any]) -> str:
    return sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _event_key(body: dict[str, Any]) -> str:
    metadata = body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("uid") or f"{metadata.get('namespace')}/{metadata.get('name')}")


def _poll_payload(alert: dict[str, Any]) -> dict[str, Any] | None:
    """Alertmanager ``/api/v2/alerts`` shape to the webhook payload shape; active alerts only."""
    status = alert.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    if state != "active":
        return None
    return {
        "status": "firing",
        "labels": alert.get("labels") or {},
        "annotations": alert.get("annotations") or {},
        "startsAt": alert.get("startsAt"),
        "endsAt": alert.get("endsAt"),
        "fingerprint": alert.get("fingerprint"),
        "generatorURL": alert.get("generatorURL"),
    }


@dataclass(frozen=True)
class AuditEntry:
    """One served request: what was asked (as a digest), how much came back, the outcome."""

    op: str
    request_digest: str
    response_bytes: int
    outcome: str
    at: datetime


@dataclass
class Connector:
    cluster: ClusterReader | None = None
    providers: ProviderReaders = field(default_factory=ProviderReaders)
    audit: Callable[[AuditEntry], None] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    watch_namespaces: tuple[str, ...] = ()
    alert_source: AlertSource | None = None
    accept_webhook: bool = False
    alerts_buffer_len: int = 10_000
    changes_buffer_len: int = 50_000
    epoch: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        self._alerts = StreamBuffer(self.epoch, self.alerts_buffer_len, self.clock)
        self._changes = StreamBuffer(self.epoch, self.changes_buffer_len, self.clock)
        self._known_alerts: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, str] = {}
        self._events_seen: dict[str, str] = {}
        self._changes_lock = threading.RLock()

    def handle(self, payload: bytes) -> bytes:
        """Serve one request and return the encoded response; never raises."""
        digest = sha256(payload).hexdigest()
        op = "invalid"
        try:
            request = wire.loads_request(payload)
            op = request.op
            if request.version != wire.WIRE_VERSION:
                response = _failure(
                    "UnsupportedVersion", f"expected {wire.WIRE_VERSION}, got {request.version}"
                )
            else:
                response = wire.Response(
                    version=wire.WIRE_VERSION, ok=True, result=self._dispatch(request)
                )
        except (ValidationError, InvalidArguments) as error:
            response = _failure("InvalidRequest", _short(str(error)))
        except Exception as error:  # a backend failure is reported, never swallowed
            response = _failure(type(error).__name__, _short(str(error)))
        try:
            encoded = wire.dumps(response)
        except (TypeError, ValueError) as error:
            encoded = wire.dumps(_failure("EncodingError", _short(str(error))))
        if len(encoded) > wire.MAX_RESPONSE_BYTES:
            encoded = wire.dumps(
                _failure("ResponseTooLarge", f"{len(encoded)} bytes exceed the connector limit")
            )
        if self.audit is not None:
            self.audit(
                AuditEntry(
                    op=op,
                    request_digest=digest,
                    response_bytes=len(encoded),
                    outcome="ok" if wire.loads_response(encoded).ok else "failure",
                    at=self.clock(),
                )
            )
        return encoded

    def capabilities(self) -> list[str]:
        names = []
        if self.cluster is not None:
            names += ["history", "events"]
        if self.providers.loki is not None:
            names.append("logs")
        if self.providers.prometheus is not None:
            names += ["resource_pressure", "traffic"]
        if self.providers.tempo is not None:
            names.append("runtime_traces")
        if self.cluster is not None:
            names.append("changes")
        if self.alert_source is not None or self.accept_webhook:
            names.append("alerts")
        return sorted(names)

    # ---- alert stream ---------------------------------------------------------------------

    def _put(self, buffer: StreamBuffer, model: wire.WireModel) -> int:
        return buffer.append(model.model_dump(mode="json"))

    def poll_alerts_once(self) -> None:
        """One Alertmanager poll: occurrences that appeared or resolved, then its heartbeat."""
        if self.alert_source is None:
            return
        attempted = self.clock()
        try:
            alerts = self.alert_source.list_alerts()
        except Exception as error:
            completed = self.clock()
            self._put(
                self._alerts,
                wire.HeartbeatItem(
                    seq=0,
                    attempted_at=attempted,
                    completed_at=completed,
                    ok=False,
                    error_type=type(error).__name__,
                ),
            )
            self._put(self._alerts, wire.GapItem(seq=0, at=completed, reason="BACKEND_UNREACHABLE"))
            return
        completed = self.clock()
        current: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            payload = _poll_payload(alert)
            fingerprint = alert.get("fingerprint")
            if payload is not None and isinstance(fingerprint, str):
                current[fingerprint] = payload
        for fingerprint, payload in current.items():
            if fingerprint not in self._known_alerts:
                self._put(
                    self._alerts,
                    wire.AlertItem(seq=0, observed_at=completed, origin="poll", alert=payload),
                )
        for fingerprint, payload in list(self._known_alerts.items()):
            if fingerprint not in current:
                resolved = {**payload, "status": "resolved", "endsAt": completed.isoformat()}
                self._put(
                    self._alerts,
                    wire.AlertItem(seq=0, observed_at=completed, origin="poll", alert=resolved),
                )
        self._known_alerts = current
        self._put(
            self._alerts,
            wire.HeartbeatItem(
                seq=0,
                attempted_at=attempted,
                completed_at=completed,
                ok=True,
                active_alerts=len(alerts),
            ),
        )

    def receive_webhook(self, payload: dict[str, Any]) -> int:
        """An Alertmanager delivery received inside the customer environment (contract §10.5)."""
        if not self.accept_webhook:
            raise LookupError("this connector does not accept webhooks")
        webhook = AlertmanagerWebhook.model_validate(payload)
        now = self.clock()
        for alert in webhook.alerts:
            self._put(
                self._alerts,
                wire.AlertItem(
                    seq=0,
                    observed_at=now,
                    origin="webhook",
                    alert=alert.model_dump(mode="json", by_alias=True),
                ),
            )
        return len(webhook.alerts)

    # ---- change stream --------------------------------------------------------------------

    def _read_changes(self, cursor: str | None, limit: int) -> dict[str, Any]:
        with self._changes_lock:
            if not self._changes.has_baseline():
                self.poll_changes_once(force_snapshot=True)
            try:
                return self._changes.read(cursor, limit, needs_baseline=True)
            except LookupError:
                self.poll_changes_once(force_snapshot=True)
                return self._changes.read(cursor, limit, needs_baseline=True)

    def poll_changes_once(self, *, force_snapshot: bool = False) -> None:
        """List the watched namespaces and append what changed since the last listing."""
        if self.cluster is None:
            return
        with self._changes_lock:
            at = self.clock()
            try:
                listing = self.cluster.list_objects(self.watch_namespaces)
                events = self.cluster.list_events(self.watch_namespaces)
                objects = [o for o in listing.objects if o.get("kind") not in wire.DENIED_KINDS]
                if force_snapshot or not self._changes.has_baseline():
                    self._snapshot(objects, events, listing, at)
                else:
                    self._delta(objects, events, listing, at)
            except Exception:
                logger.warning("change poll failed", exc_info=True)
                self._put(self._changes, wire.GapItem(seq=0, at=at, reason="BACKEND_UNREACHABLE"))

    def _status(self, listing: ObjectListing, at: datetime, *, snapshot: bool) -> None:
        self._put(
            self._changes,
            wire.ListingStatusItem(
                seq=0,
                observed_at=at,
                snapshot=snapshot,
                completed_scopes=[
                    wire.ScopeWire(namespace=s.namespace, kind=s.kind)
                    for s in sorted(listing.completed_scopes, key=lambda s: (s.namespace, s.kind))
                ],
                failed_scopes=[
                    wire.ScopeFailureWire(
                        scope=wire.ScopeWire(namespace=f.scope.namespace, kind=f.scope.kind),
                        error=f.error,
                    )
                    for f in listing.failed_scopes
                ],
            ),
        )

    def _snapshot(
        self,
        objects: list[dict[str, Any]],
        events: Sequence[dict[str, Any]],
        listing: ObjectListing,
        at: datetime,
    ) -> None:
        if len(objects) + len(events) + 2 > self.changes_buffer_len:
            raise ValueError("snapshot exceeds the change buffer")
        self._seen, self._events_seen = {}, {}
        begin = self._put(self._changes, wire.SnapshotBeginItem(seq=0, observed_at=at))
        self._changes.mark_baseline(begin)
        for body in objects:
            key = object_key(body)
            if key is None:
                continue
            self._seen[key] = _digest(body)
            self._put(self._changes, wire.ObjectItem(seq=0, observed_at=at, body=body))
        for event in events:
            self._events_seen[_event_key(event)] = _digest(event)
            self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=event))
        self._status(listing, at, snapshot=True)

    def _delta(
        self,
        objects: list[dict[str, Any]],
        events: Sequence[dict[str, Any]],
        listing: ObjectListing,
        at: datetime,
    ) -> None:
        present: set[str] = set()
        for body in objects:
            key = object_key(body)
            if key is None:
                continue
            present.add(key)
            digest = _digest(body)
            if self._seen.get(key) != digest:
                self._seen[key] = digest
                self._put(self._changes, wire.ObjectItem(seq=0, observed_at=at, body=body))
        for key in sorted(self._seen):
            scope = ListingScope.from_key(key)
            if key not in present and scope is not None and scope in listing.completed_scopes:
                del self._seen[key]
                self._put(self._changes, wire.ObjectDeletedItem(seq=0, observed_at=at, key=key))
        for event in events:
            digest = _digest(event)
            key = _event_key(event)
            if self._events_seen.get(key) != digest:
                self._events_seen[key] = digest
                self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=event))
        self._status(listing, at, snapshot=False)

    def watch_changes_once(self) -> None:
        """Consume each scope's watch once (connector contract §15). Not implemented yet."""
        raise NotImplementedError("the watch path of contract §15 is not implemented yet")

    def run(
        self,
        stop: threading.Event,
        *,
        changes_interval: float = 15.0,
        alerts_interval: float = 60.0,
    ) -> None:
        """Poll both sources until stopped; a poll never raises out of the loop."""
        next_changes = next_alerts = 0.0
        elapsed = 0.0
        while not stop.is_set():
            if elapsed >= next_changes:
                self.poll_changes_once()
                next_changes = elapsed + changes_interval
            if elapsed >= next_alerts:
                self.poll_alerts_once()
                next_alerts = elapsed + alerts_interval
            step = min(changes_interval, alerts_interval, 1.0)
            if stop.wait(step):
                break
            elapsed += step

    def _dispatch(self, request: wire.Request) -> dict[str, Any]:
        args = request.args
        if request.op == "capabilities":
            return {
                "connector_version": CONNECTOR_VERSION,
                "wire": wire.WIRE_VERSION,
                "capabilities": self.capabilities(),
            }
        if request.op == "preflight":
            # Configured, not probed: reachability probing arrives with `connectorctl preflight`.
            backends = {
                "kubernetes": self.cluster is not None,
                "loki": self.providers.loki is not None,
                "prometheus": self.providers.prometheus is not None,
                "tempo": self.providers.tempo is not None,
            }
            return {
                "backends": [
                    {"name": name, "configured": configured, "reachable": None}
                    for name, configured in sorted(backends.items())
                ]
            }
        if request.op == "read_alerts":
            if self.alert_source is None and not self.accept_webhook:
                raise LookupError("alertmanager backend is not configured")
            read = _args(wire.ReadArgs, args)
            return self._alerts.read(read.cursor, read.limit, needs_baseline=False)
        if request.op == "read_changes":
            if self.cluster is None:
                raise LookupError("kubernetes backend is not configured")
            read = _args(wire.ReadArgs, args)
            return self._read_changes(read.cursor, read.limit)
        if request.op in {"list_objects", "list_events"}:
            if self.cluster is None:
                raise LookupError("kubernetes backend is not configured")
            namespaces = _args(wire.NamespaceArgs, args).namespaces
            if request.op == "list_events":
                return {"events": list(self.cluster.list_events(namespaces))}
            listing = self.cluster.list_objects(namespaces)
            allowed = tuple(o for o in listing.objects if o.get("kind") not in wire.DENIED_KINDS)
            return wire.listing_to_wire(
                ObjectListing(allowed, listing.completed_scopes, listing.failed_scopes)
            )
        if request.op == "query_logs":
            logs = _args(wire.LogArgs, args)
            if self.providers.loki is None:
                raise LookupError("loki backend is not configured")
            return wire.records_to_wire(
                "logs",
                self.providers.loki.error_logs(
                    logs.services, logs.starts_at, logs.ends_at, limit=logs.limit
                ),
            )
        query = _args(wire.TargetQueryArgs, args)
        if request.op == "query_traces":
            if self.providers.tempo is None:
                raise LookupError("tempo backend is not configured")
            return wire.traces_to_wire(self.providers.tempo.query(query.target, query.query))
        if self.providers.prometheus is None:
            raise LookupError("prometheus backend is not configured")
        if request.op == "query_resource_pressure":
            return wire.records_to_wire(
                "pressure",
                self.providers.prometheus.query_resource_pressure(query.target, query.query),
            )
        return wire.records_to_wire(
            "traffic", self.providers.prometheus.query_traffic(query.target, query.query)
        )


def _failure(error_type: str, message: str) -> wire.Response:
    return wire.Response(
        version=wire.WIRE_VERSION,
        ok=False,
        failure=wire.Failure(error_type=error_type, message=message),
    )


def _short(message: str) -> str:
    return message[:_MESSAGE_LIMIT]


@dataclass(frozen=True)
class AlertmanagerAlerts:
    """``AlertSource`` over the read-only Alertmanager client (``GET /api/v2/alerts`` only)."""

    reader: AlertmanagerReader

    def list_alerts(self) -> list[dict[str, Any]]:
        return fetch_alerts(self.reader)


def connector_from_environment(
    chaos_namespaces: tuple[str, ...],
    watch_namespaces: tuple[str, ...] = (),
    *,
    accept_webhook: bool = False,
) -> Connector:
    """The customer-side readers, configured exactly as the control plane used to configure them."""
    alertmanager = AlertmanagerConfig.from_environment()
    return Connector(
        watch_namespaces=watch_namespaces,
        accept_webhook=accept_webhook,
        alert_source=(
            AlertmanagerAlerts(AlertmanagerReader(alertmanager))
            if alertmanager is not None
            else None
        ),
        cluster=(
            KubernetesClusterReader(chaos_namespaces=chaos_namespaces)
            if os.getenv("SRE_CLUSTER_ACCESS") == "true"
            else None
        ),
        providers=ProviderReaders.from_environment(),
    )
