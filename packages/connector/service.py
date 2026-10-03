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
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from packages.connector import wire
from packages.connector.streams import StreamBuffer
from packages.connector.watch import ResourceVersionExpired, WatchEvent
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
# contract §15.6: kinds the API server sends no bookmarks for, and how often to learn a newer version
SYNTHETIC_BOOKMARK_KINDS = frozenset({"Event"})
SYNTHETIC_BOOKMARK_SECONDS = 120.0
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
    # contract §16: the poll admits a firing occurrence only once it has been active this long, so both alert
    # paths agree on a short alert (Alertmanager notifies a group only after its group_wait)
    alert_min_active: timedelta = timedelta(0)
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
        # the resourceVersion each scope's watch resumes from (contract §15); set by every listing
        self._versions: dict[ListingScope, str] = {}
        self._generation = 0
        self._degraded = False  # a watch failed; the run loop retries the snapshot
        self._pending_relists: set[ListingScope] = set()  # failed scope relists (contract §15.4)
        # the last instant each scope was observed continuously (late-evidence-design.md §4.2)
        self._continuous_until: dict[ListingScope, datetime] = {}
        # contract §15.6: the watch open per scope, and a synthetic bookmark held for that watch
        self._open_watches: dict[ListingScope, int] = {}
        self._watch_ids = 0
        self._synthetic: dict[ListingScope, tuple[int, str]] = {}
        self.watch_stats: dict[str, int] = defaultdict(int)

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

    def _admissible(self, fingerprint: str, payload: dict[str, Any], now: datetime) -> bool:
        known = self._known_alerts.get(fingerprint)
        if known is not None and known.get("startsAt") == payload.get("startsAt"):
            return True  # already admitted, still firing
        if self.alert_min_active <= timedelta(0):
            return True
        starts = payload.get("startsAt")
        if not isinstance(starts, str):
            return True  # no start to age from: admitted as before
        try:
            began = datetime.fromisoformat(starts.replace("Z", "+00:00"))
        except ValueError:
            return True
        if began.tzinfo is None:
            began = began.replace(tzinfo=UTC)
        return now - began >= self.alert_min_active

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
        # contract §16: an occurrence younger than ``alert_min_active`` is not admitted yet; a later poll admits
        # it if it is still firing, and one that resolves young is never reported
        admitted = {
            fingerprint: payload
            for fingerprint, payload in current.items()
            if self._admissible(fingerprint, payload, completed)
        }
        for fingerprint, payload in list(self._known_alerts.items()):
            later = current.get(fingerprint)
            if later is not None and later.get("startsAt") == payload.get("startsAt"):
                continue  # the same occurrence, still firing
            # Gone, or replaced: Alertmanager holds one alert per fingerprint, so a new start means
            # this occurrence ended, at the latest when the new one began.
            ended = later.get("startsAt") if later is not None else None
            resolved = {
                **payload,
                "status": "resolved",
                "endsAt": ended if isinstance(ended, str) else completed.isoformat(),
            }
            self._put(
                self._alerts,
                wire.AlertItem(seq=0, observed_at=completed, origin="poll", alert=resolved),
            )
        for fingerprint, payload in admitted.items():
            known = self._known_alerts.get(fingerprint)
            if known is None or known.get("startsAt") != payload.get("startsAt"):
                self._put(
                    self._alerts,
                    wire.AlertItem(seq=0, observed_at=completed, origin="poll", alert=payload),
                )
        self._known_alerts = admitted
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

    def poll_changes_once(
        self, *, force_snapshot: bool = False, gap_on_failure: bool = True
    ) -> None:
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
                if gap_on_failure:
                    self._put(self._changes, self._global_gap(at, "BACKEND_UNREACHABLE"))

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

    def _count_bytes(self, body: dict[str, Any]) -> None:
        self.watch_stats["bytes"] += len(json.dumps(body, separators=(",", ":"), default=str))

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
        self._versions = {**listing.resource_versions, **self._event_versions()}
        self._continuous_until = dict.fromkeys(self._versions, at)
        self._generation += 1  # watches of the replaced versions must not touch the new state
        self._pending_relists = set()  # a global snapshot relists every scope
        # a scope that failed, or a watched namespace whose Events returned no version, has no watch:
        # keep retrying until every scope is listed, rather than leave it unwatched until reconciliation
        self._degraded = self._incomplete(listing)
        begin = self._put(self._changes, wire.SnapshotBeginItem(seq=0, observed_at=at))
        self._changes.mark_baseline(begin)
        for body in objects:
            key = object_key(body)
            if key is None:
                continue
            self._seen[key] = _digest(body)
            self._count_bytes(body)
            self._put(self._changes, wire.ObjectItem(seq=0, observed_at=at, body=body))
        for event in events:
            self._events_seen[_event_key(event)] = _digest(event)
            self._count_bytes(event)
            self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=event))
        self._status(listing, at, snapshot=True)

    def _delta(
        self,
        objects: list[dict[str, Any]],
        events: Sequence[dict[str, Any]],
        listing: ObjectListing,
        at: datetime,
    ) -> None:
        self._versions.update({**listing.resource_versions, **self._event_versions()})
        for completed in listing.completed_scopes:
            self._continuous_until[completed] = at
        present: set[str] = set()
        for body in objects:
            key = object_key(body)
            if key is None:
                continue
            present.add(key)
            digest = _digest(body)
            if self._seen.get(key) != digest:
                self._seen[key] = digest
                self._count_bytes(body)
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
                self._count_bytes(event)
                self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=event))
        self._status(listing, at, snapshot=False)

    def watch_changes_once(self) -> None:
        """Consume each scope's watch until the server ends it, once per scope (contract §15).

        Synchronous: the background loop runs the same per-scope step in its own threads.
        """
        for scope in list(self._versions):
            if not self.watch_scope_once(scope):
                return  # continuity was lost; the new snapshot set every scope's version again

    def _incomplete(self, listing: ObjectListing) -> bool:
        if getattr(self.cluster, "watch", None) is None:
            return False  # a polling cluster: the next poll is the retry
        if listing.failed_scopes:
            return True
        events = {scope.namespace for scope in self._versions if scope.kind == "Event"}
        return bool(self._event_versions()) and not set(self.watch_namespaces) <= events

    def _event_versions(self) -> dict[ListingScope, str]:
        versions = getattr(self.cluster, "event_resource_versions", None)
        return dict(versions) if isinstance(versions, dict) else {}

    def watch_scope_once(self, scope: ListingScope) -> bool:
        """Consume one scope's watch until it ends; False when continuity was lost (a gap and a snapshot)."""
        with self._changes_lock:
            self._watch_ids += 1
            watch_id = self._watch_ids
            self._open_watches[scope] = watch_id
            self._synthetic.pop(scope, None)
        try:
            return self._watch_scope(scope, watch_id)
        finally:
            with self._changes_lock:
                if self._open_watches.get(scope) == watch_id:
                    del self._open_watches[scope]
                held = self._synthetic.get(scope)
                if held is not None and held[0] == watch_id:
                    del self._synthetic[scope]  # not adopted at a normal end: discarded

    def refresh_bookmarks_once(self) -> None:
        """Synthetic bookmarks (contract §15.6): a newer resume version for each open watch of a scope
        that receives no real bookmarks, learned from a ``limit=1`` LIST while that watch is open."""
        read_version = getattr(self.cluster, "scope_version", None)
        if read_version is None:
            return
        with self._changes_lock:
            open_watches = [
                (scope, watch_id)
                for scope, watch_id in self._open_watches.items()
                if scope.kind in SYNTHETIC_BOOKMARK_KINDS
            ]
        for scope, watch_id in open_watches:
            try:
                version = str(read_version(scope))
            except Exception:
                logger.warning(
                    "synthetic bookmark of %s/%s failed", scope.namespace, scope.kind, exc_info=True
                )
                continue
            with self._changes_lock:
                # open before and after the LIST, and the same watch: the LIST ran while it was open
                if self._open_watches.get(scope) == watch_id:
                    self._synthetic[scope] = (watch_id, version)
                    self.watch_stats["synthetic_bookmarks"] += 1

    def _watch_scope(self, scope: ListingScope, watch_id: int) -> bool:
        watch = getattr(self.cluster, "watch", None)
        with self._changes_lock:
            version = self._versions.get(scope)
            generation = self._generation
            waiting = self._degraded or scope in self._pending_relists
        if watch is None or version is None or waiting:
            return not waiting
        try:
            for event in watch(scope, version):
                if not self._apply_watch_event(scope, event, generation):
                    return (
                        True  # a newer snapshot replaced this watch; the caller starts a fresh one
                    )
        except ResourceVersionExpired:
            with self._changes_lock:
                if generation != self._generation:
                    return True  # another scope's expiry already took the new snapshot
                logger.warning(
                    "watch of %s/%s expired at %s; scope gap and relist",
                    scope.namespace,
                    scope.kind,
                    version,
                )
                self.watch_stats["expired"] += 1
                self.watch_stats[f"expired:{scope.kind}"] += 1
                if getattr(self.cluster, "list_scope", None) is None:
                    # a reader that cannot list one scope: the global gap and snapshot of §15
                    self._put(
                        self._changes, self._global_gap(self.clock(), "RESOURCE_VERSION_EXPIRED")
                    )
                    self.watch_stats["relists"] += 1
                    self.poll_changes_once(force_snapshot=True)
                    if self._generation == generation:
                        self._degraded = True
                    return False
                # contract §15.4: the gap and the relist belong to this scope alone
                self._put(
                    self._changes,
                    wire.GapItem(
                        seq=0,
                        at=self.clock(),
                        reason="RESOURCE_VERSION_EXPIRED",
                        scope=wire.ScopeWire(namespace=scope.namespace, kind=scope.kind),
                        since=self._continuous_until.get(scope),
                    ),
                )
                if self._relist_scope(scope):
                    return True
                self._pending_relists.add(scope)
            return False
        except Exception:
            with self._changes_lock:
                if generation != self._generation:
                    return True
                self.watch_stats["watch_failures"] += 1
                if not self._degraded:
                    # one gap for the outage; the run loop retries the snapshot, not every watch
                    logger.warning(
                        "watch of %s/%s failed", scope.namespace, scope.kind, exc_info=True
                    )
                    self._degraded = True
                    self._put(self._changes, self._global_gap(self.clock(), "BACKEND_UNREACHABLE"))
            return False
        with self._changes_lock:
            if generation == self._generation and scope in self._versions:
                # the server ended the watch normally: the scope was observed without a break until now
                self._continuous_until[scope] = self.clock()
                held = self._synthetic.pop(scope, None)
                current = self._versions[scope]
                if held is not None and held[0] == watch_id and _newer(held[1], current):
                    self._versions[scope] = held[1]  # contract §15.6: resume from the newer version
                    self.watch_stats["synthetic_adopted"] += 1
        self.watch_stats["resumes"] += 1
        return True

    def _global_gap(self, at: datetime, reason: wire.GapReason) -> wire.GapItem:
        # conservative start: the earliest instant up to which every scope was observed continuously
        return wire.GapItem(
            seq=0, at=at, reason=reason, since=min(self._continuous_until.values(), default=None)
        )

    def _apply_watch_event(self, scope: ListingScope, event: WatchEvent, generation: int) -> bool:
        """Apply one watch event; False when a newer snapshot has replaced the watch it came from."""
        with self._changes_lock:
            if generation != self._generation or self._versions.get(scope) is None:
                return False
            self._versions[scope] = event.resource_version
            self._continuous_until[scope] = self.clock()
            self.watch_stats["events"] += 1
            body = event.body
            if event.type == "BOOKMARK" or body.get("kind") in wire.DENIED_KINDS:
                return True
            at = self.clock()
            if scope.kind == "Event":
                key, digest = _event_key(body), _digest(body)
                if self._events_seen.get(key) != digest:
                    self._events_seen[key] = digest
                    self._count_bytes(body)
                    self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=body))
                return True
            object_id = object_key(body)
            if object_id is None:
                return True
            if event.type == "DELETED":
                # observed deletion: a continuous watch (a gap would have replaced this version)
                if self._seen.pop(object_id, None) is not None:
                    self._put(
                        self._changes,
                        wire.ObjectDeletedItem(
                            seq=0, observed_at=at, key=object_id, source="watch"
                        ),
                    )
                return True
            digest = _digest(body)
            if self._seen.get(object_id) != digest:
                self._seen[object_id] = digest
                self._count_bytes(body)
                self._put(self._changes, wire.ObjectItem(seq=0, observed_at=at, body=body))
            return True

    def _relist_scope(self, scope: ListingScope) -> bool:
        """List one scope again and serve it as that scope's snapshot (contract §15.4); False if it failed.

        Every current item of the scope is emitted, so the mirror can replace exactly that scope; what
        happened while continuity was lost is not reconstructed.
        """
        self.watch_stats["scope_relists"] += 1
        try:
            items, version = self.cluster.list_scope(scope)  # type: ignore[union-attr]
        except Exception:
            logger.warning("relist of %s/%s failed", scope.namespace, scope.kind, exc_info=True)
            return False
        at = self.clock()
        scope_wire = wire.ScopeWire(namespace=scope.namespace, kind=scope.kind)
        self._put(self._changes, wire.SnapshotBeginItem(seq=0, observed_at=at, scope=scope_wire))
        if scope.kind == "Event":
            for body in items:
                self._events_seen[_event_key(body)] = _digest(body)
                self._count_bytes(body)
                self._put(self._changes, wire.EventItem(seq=0, observed_at=at, body=body))
        else:
            for key in [k for k in self._seen if ListingScope.from_key(k) == scope]:
                del self._seen[key]
            for body in items:
                object_id = object_key(body)
                if object_id is None or body.get("kind") in wire.DENIED_KINDS:
                    continue
                self._seen[object_id] = _digest(body)
                self._count_bytes(body)
                self._put(self._changes, wire.ObjectItem(seq=0, observed_at=at, body=body))
        self._put(
            self._changes,
            wire.ListingStatusItem(
                seq=0,
                observed_at=at,
                snapshot=True,
                completed_scopes=[scope_wire],
                failed_scopes=[],
                scope=scope_wire,
            ),
        )
        self._versions[scope] = version
        self._continuous_until[scope] = at
        self._pending_relists.discard(scope)
        return True

    def change_heartbeat(self) -> None:
        """Stamp the change stream with the Connector's time (late-evidence-design.md §4.1)."""
        self._put(self._changes, wire.ChangeHeartbeatItem(seq=0, observed_at=self.clock()))

    def retry_snapshot_once(self) -> bool:
        """After a watch failure or a failed scope relist, try again; whether a retry was due."""
        with self._changes_lock:
            if self._degraded:
                self.watch_stats["relists"] += 1
                self.poll_changes_once(force_snapshot=True, gap_on_failure=False)
                return True
            if not self._pending_relists:
                return False
            for scope in sorted(self._pending_relists, key=lambda s: (s.namespace, s.kind)):
                self._relist_scope(scope)
            return True

    def _watch_loop(self, scope: ListingScope, stop: threading.Event) -> None:
        """Keep one scope watched: resume after the server ends a watch, start over after a snapshot."""
        while not stop.is_set():
            with self._changes_lock:
                if scope not in self._versions:
                    return
                generation = self._generation
            try:
                if not self.watch_scope_once(scope):
                    # wait for the loop's retry: a global snapshot, or this scope's relist
                    while (
                        not stop.is_set()
                        and self._generation == generation
                        and (self._degraded or scope in self._pending_relists)
                    ):
                        stop.wait(1.0)
            except Exception:
                logger.warning("watch loop of %s/%s", scope.namespace, scope.kind, exc_info=True)
                stop.wait(5.0)

    def run(
        self,
        stop: threading.Event,
        *,
        changes_interval: float = 15.0,
        alerts_interval: float = 60.0,
        watch: bool = True,
        reconcile_interval: float = 600.0,
    ) -> None:
        """Poll both sources until stopped; a poll never raises out of the loop.

        With a cluster that can watch (contract §15), each listed scope is watched in its own thread
        and the full listing becomes a reconciliation every ``reconcile_interval`` seconds.
        """
        watching = watch and getattr(self.cluster, "watch", None) is not None
        if watching:
            changes_interval = reconcile_interval
        threads: dict[ListingScope, threading.Thread] = {}
        next_changes = next_alerts = next_stats = next_retry = next_beat = next_bookmark = 0.0
        started = time.monotonic()  # wall time, so the periods do not drift (contract §15.6)
        while not stop.is_set():
            elapsed = time.monotonic() - started
            if elapsed >= next_changes:
                self.poll_changes_once()
                next_changes = elapsed + changes_interval
            if watching:
                with self._changes_lock:
                    scopes = list(self._versions)
                for scope in scopes:
                    thread = threads.get(scope)
                    if thread is None or not thread.is_alive():
                        thread = threading.Thread(
                            target=self._watch_loop, args=(scope, stop), daemon=True
                        )
                        threads[scope] = thread
                        thread.start()
                        self.watch_stats["watch_starts"] += 1
            if watching and elapsed >= next_bookmark:
                self.refresh_bookmarks_once()
                next_bookmark = elapsed + SYNTHETIC_BOOKMARK_SECONDS
            if watching and elapsed >= next_beat:
                self.change_heartbeat()  # proves transport on a quiet stream (late-evidence §4.1)
                next_beat = elapsed + 2.0
            if watching and elapsed >= next_retry and self.retry_snapshot_once():
                next_retry = (
                    elapsed + 5.0
                )  # a watch failed: retry the snapshot, not in a tight loop
            if elapsed >= next_stats:
                next_stats = elapsed + 60.0
                live = sum(1 for t in threads.values() if t.is_alive())
                calls = dict(getattr(self.cluster, "api_calls", {}) or {})
                logger.info("watch stats: active=%d api=%s %s", live, calls, dict(self.watch_stats))
            if elapsed >= next_alerts:
                self.poll_alerts_once()
                next_alerts = elapsed + alerts_interval
            step = min(changes_interval, alerts_interval, 1.0)
            if stop.wait(step):
                break

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


def _newer(candidate: str, current: str) -> bool:
    """Whether a resource version is later than another; opaque (non-numeric) versions never are."""
    try:
        return int(candidate) > int(current)
    except ValueError:
        return False


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
        alert_min_active=timedelta(seconds=float(os.getenv("SRE_ALERT_MIN_ACTIVE_SECONDS", "30"))),
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
