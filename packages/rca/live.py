"""Live cluster access: read-only object listing, change journal, and incident source."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from packages.rca.json_access import child, object_content_hash
from packages.rca.model import (
    CLUSTER_SCOPE,
    Alert,
    ClusterEvent,
    EntityRef,
    JournalEntry,
    Lifecycle,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
)

# (API group client attribute, list method, kind). Secrets are deliberately not read.
_NAMESPACED_LISTS: tuple[tuple[str, str, str], ...] = (
    ("CoreV1Api", "list_namespaced_config_map", "ConfigMap"),
    ("CoreV1Api", "list_namespaced_service", "Service"),
    ("CoreV1Api", "list_namespaced_pod", "Pod"),
    ("CoreV1Api", "list_namespaced_resource_quota", "ResourceQuota"),
    ("CoreV1Api", "list_namespaced_limit_range", "LimitRange"),
    ("AppsV1Api", "list_namespaced_deployment", "Deployment"),
    ("AppsV1Api", "list_namespaced_stateful_set", "StatefulSet"),
    ("AppsV1Api", "list_namespaced_daemon_set", "DaemonSet"),
    ("AppsV1Api", "list_namespaced_replica_set", "ReplicaSet"),
    ("NetworkingV1Api", "list_namespaced_network_policy", "NetworkPolicy"),
    ("AutoscalingV2Api", "list_namespaced_horizontal_pod_autoscaler", "HorizontalPodAutoscaler"),
)
_CHAOS_PLURALS = (
    ("NetworkChaos", "networkchaos"),
    ("PodChaos", "podchaos"),
    ("StressChaos", "stresschaos"),
    ("IOChaos", "iochaos"),
    ("HTTPChaos", "httpchaos"),
    ("Schedule", "schedules"),
)
_LOG_ERRORS = "(?i)(error|exception|fatal|refused|timeout|unavailable|unreachable)"


class ClusterReader(Protocol):
    """Read-only view of the cluster."""

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing: ...

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]: ...


class LogReader(Protocol):
    def error_logs(
        self, services: Sequence[str], starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord]: ...


@dataclass(frozen=True)
class ListingScope:
    """The smallest scope for which absence can imply deletion."""

    namespace: str
    kind: str

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> ListingScope | None:
        metadata = child(body, "metadata")
        kind = body.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        return cls(str(metadata.get("namespace") or CLUSTER_SCOPE), kind)

    @classmethod
    def from_key(cls, key: str) -> ListingScope | None:
        parts = key.split("/", 2)
        if len(parts) != 3:
            return None
        return cls(parts[0], parts[1])


@dataclass(frozen=True)
class ListingFailure:
    """A resource scope that could not be enumerated."""

    scope: ListingScope
    error: str


@dataclass(frozen=True)
class ObjectListing:
    """Object bodies plus explicit enumeration completeness metadata."""

    objects: tuple[dict[str, Any], ...]
    completed_scopes: frozenset[ListingScope]
    failed_scopes: tuple[ListingFailure, ...] = ()


@dataclass(frozen=True)
class ObjectSnapshot:
    """One object-journal cycle and the boundary at which it was captured."""

    listing: ObjectListing
    stored_versions: int
    started_at: datetime
    completed_at: datetime


class KubernetesClusterReader:
    """Lists objects and events with the Kubernetes Python client (read verbs only)."""

    def __init__(self, *, chaos_namespaces: Sequence[str] = ("chaos-mesh",)) -> None:
        self.chaos_namespaces = tuple(chaos_namespaces)
        self._module: Any | None = None
        self._api_client: Any | None = None

    def _client(self) -> tuple[Any, Any]:
        if self._module is None:
            kubernetes = importlib.import_module("kubernetes")
            config = importlib.import_module("kubernetes.config")
            try:
                config.load_incluster_config()
            except Exception:  # not running in a pod
                config.load_kube_config()
            self._module = kubernetes
            self._api_client = kubernetes.client.ApiClient()
        return self._module, self._api_client

    def _serialize(self, item: Any, kind: str, api_version: str) -> dict[str, Any]:
        _module, api_client = self._client()
        body: dict[str, Any] = api_client.sanitize_for_serialization(item)
        body["kind"] = kind
        body.setdefault("apiVersion", api_version)
        child(body, "metadata").pop("managedFields", None)
        return body

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        kubernetes, _api_client = self._client()
        objects: list[dict[str, Any]] = []
        completed: set[ListingScope] = set()
        failures: list[ListingFailure] = []
        workload_namespaces = [
            namespace for namespace in namespaces if namespace not in self.chaos_namespaces
        ]
        for group, method, kind in _NAMESPACED_LISTS:
            for namespace in workload_namespaces:
                scope = ListingScope(namespace, kind)
                try:
                    api = getattr(kubernetes.client, group)()
                    items = getattr(api, method)(namespace).items
                except Exception as exc:
                    failures.append(ListingFailure(scope, f"{type(exc).__name__}: {exc}"))
                    continue
                completed.add(scope)
                objects.extend(self._serialize(item, kind, "v1") for item in items)
        custom = kubernetes.client.CustomObjectsApi()
        for namespace in self.chaos_namespaces:
            for kind, plural in _CHAOS_PLURALS:
                scope = ListingScope(namespace, kind)
                try:
                    listing = custom.list_namespaced_custom_object(
                        "chaos-mesh.org", "v1alpha1", namespace, plural
                    )
                except Exception as exc:  # CRD not installed or not readable
                    failures.append(ListingFailure(scope, f"{type(exc).__name__}: {exc}"))
                    continue
                completed.add(scope)
                for item in listing.get("items", []):
                    item["kind"] = kind
                    child(item, "metadata").pop("managedFields", None)
                    objects.append(item)
        return ObjectListing(tuple(objects), frozenset(completed), tuple(failures))

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        kubernetes, _api_client = self._client()
        core = kubernetes.client.CoreV1Api()
        events: list[dict[str, Any]] = []
        for namespace in dict.fromkeys((*namespaces, *self.chaos_namespaces)):
            try:
                items = core.list_namespaced_event(namespace).items
            except Exception:
                continue
            events.extend(self._serialize(item, "Event", "v1") for item in items)
        return events


@dataclass
class LokiLogReader:
    """Warning and error lines from Loki for the incident window."""

    base_url: str
    timeout_seconds: float = 5.0
    limit: int = 500
    opener: Callable[..., Any] = urlopen

    def error_logs(
        self, services: Sequence[str], starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord]:
        names = sorted({name for name in services if re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)})
        if not names:
            return []
        selector = "|".join(name.replace(".", "\\.") for name in names)
        params = {
            "query": f'{{service_name=~"{selector}"}} |~ "{_LOG_ERRORS}"',
            "start": str(int(starts_at.timestamp() * 1e9)),
            "end": str(int(ends_at.timestamp() * 1e9)),
            "limit": str(self.limit),
        }
        request = Request(
            f"{self.base_url.rstrip('/')}/loki/api/v1/query_range?{urlencode(params)}",
            method="GET",
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read(10_000_001))
        records: list[LogRecord] = []
        for stream in child(payload, "data").get("result") or []:
            labels = child(stream, "stream")
            service = labels.get("service_name") or labels.get("app") or labels.get("container")
            if not service:
                continue
            for index, entry in enumerate(stream.get("values") or []):
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                records.append(
                    LogRecord(
                        service=str(service),
                        at=datetime.fromtimestamp(int(entry[0]) / 1e9, tz=UTC),
                        severity=str(labels.get("level") or labels.get("detected_level") or ""),
                        message=str(entry[1])[:300],
                        evidence_id=f"loki:{service}:{entry[0]}:{index}",
                    )
                )
        return records


def _entity(body: Mapping[str, Any]) -> EntityRef | None:
    metadata = child(body, "metadata")
    kind, name = body.get("kind"), metadata.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        return None
    return EntityRef(
        kind=kind, name=name, namespace=str(metadata.get("namespace") or CLUSTER_SCOPE)
    )


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def events_from_bodies(bodies: Sequence[Mapping[str, Any]]) -> list[ClusterEvent]:
    events: list[ClusterEvent] = []
    for index, body in enumerate(bodies):
        involved = child(body, "involvedObject")
        kind, name = involved.get("kind"), involved.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            continue
        first = _time(body.get("firstTimestamp")) or _time(body.get("eventTime"))
        events.append(
            ClusterEvent(
                entity=EntityRef(
                    kind=kind, name=name, namespace=str(involved.get("namespace") or CLUSTER_SCOPE)
                ),
                reason=str(body.get("reason") or ""),
                type=str(body.get("type") or "Normal"),
                message=str(body.get("message") or "")[:500],
                first_at=first,
                last_at=_time(body.get("lastTimestamp")) or first,
                count=int(body.get("count") or 1),
                evidence_id=f"event:{child(body, 'metadata').get('uid') or index}",
            )
        )
    return events


@dataclass
class ChangeWatcher:
    """Stores a version of every listed object that changed, and tombstones the rest.

    ``live_keys`` returns every object key the journal currently considers live
    (its last version is not a tombstone); anything missing from this listing is
    recorded as deleted with ``tombstone``.
    """

    reader: ClusterReader
    namespaces: tuple[str, ...]
    record: Callable[[dict[str, Any], datetime], bool]
    live_keys: Callable[[], set[str]] = lambda: set()
    tombstone: Callable[[str, datetime], bool] = lambda key, at: False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    last_listing: ObjectListing | None = field(default=None, init=False)

    def snapshot(self) -> int:
        return self.snapshot_result().stored_versions

    def snapshot_result(self) -> ObjectSnapshot:
        started_at = self.clock()
        previous_live = self.live_keys()
        raw_listing = self.reader.list_objects(self.namespaces)
        if isinstance(raw_listing, ObjectListing):
            listing = raw_listing
        else:
            # Compatibility for small test/demo readers that predate explicit
            # completeness metadata. Treat scopes represented by either the
            # returned objects or previously live keys as complete. Production
            # Kubernetes readers always return ObjectListing.
            bodies = tuple(raw_listing)
            scopes = {
                scope
                for scope in (
                    [ListingScope.from_body(body) for body in bodies]
                    + [ListingScope.from_key(key) for key in previous_live]
                )
                if scope is not None
            }
            listing = ObjectListing(bodies, frozenset(scopes))
        self.last_listing = listing
        observed_at = self.clock()
        seen: set[str] = set()
        stored = 0
        for body in listing.objects:
            entity = _entity(body)
            if entity is None:
                continue
            seen.add(entity.canonical)
            stored += self.record(body, observed_at)
        # Absence is meaningful only for a scope whose enumeration completed.
        # A failed API/RBAC call is missing evidence, never OBJECT_DELETED.
        missing = set()
        for key in previous_live - seen:
            scope = ListingScope.from_key(key)
            if scope is not None and scope in listing.completed_scopes:
                missing.add(key)
        stored += sum(self.tombstone(key, observed_at) for key in missing)
        completed_at = self.clock()
        return ObjectSnapshot(listing, stored, started_at, completed_at)


@dataclass
class LiveSource:
    """Observation source for one live incident.

    ``history`` is the stored object journal (oldest first); the current cluster
    state is appended as the latest version so topology reflects reality.
    """

    incident: str
    alert_items: list[Alert]
    journal: Sequence[JournalEntry]
    current_objects: list[dict[str, Any]]
    event_bodies: list[dict[str, Any]]
    error_items: list[LogRecord] = field(default_factory=list)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # False when ``current_objects`` was not fetched (e.g. a resolved incident,
    # whose window is frozen): an empty, non-live list must not be read as
    # "the cluster has none of these objects any more".
    current_is_live: bool = True

    def incident_id(self) -> str:
        return self.incident

    def observation_cutoff(self) -> datetime | None:
        """Return the diagnosis snapshot or frozen resolution boundary."""
        return self.observed_at

    def alerts(self) -> Sequence[Alert]:
        return self.alert_items

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        history: dict[EntityRef, list[ObjectVersion]] = {}
        hashes: dict[EntityRef, str] = {}
        live: set[EntityRef] = set()

        def add(body: dict[str, Any], at: datetime, evidence: str, lifecycle: Lifecycle) -> None:
            entity = _entity(body)
            if entity is None:
                return
            digest = object_content_hash(body)
            # A tombstone always lands, even with the same content as the last version.
            if lifecycle is not Lifecycle.DELETED and hashes.get(entity) == digest:
                return
            hashes[entity] = digest
            history.setdefault(entity, []).append(
                ObjectVersion(
                    entity=entity,
                    observed_at=at,
                    body=body,
                    evidence_id=evidence,
                    lifecycle=lifecycle,
                )
            )

        for entry in self.journal:
            add(entry.body, entry.observed_at, f"journal:{entry.version_id}", entry.lifecycle)
        for body in self.current_objects:
            entity = _entity(body)
            if entity is not None:
                live.add(entity)
            add(body, self.observed_at, "cluster:current", Lifecycle.UPDATED)
        # An object the journal still shows live but the cluster no longer has is deleted.
        if self.current_is_live:
            for entity, versions in history.items():
                if entity not in live and versions[-1].lifecycle is not Lifecycle.DELETED:
                    versions.append(
                        ObjectVersion(
                            entity=entity,
                            observed_at=self.observed_at,
                            body=versions[-1].body,
                            evidence_id="cluster:missing",
                            lifecycle=Lifecycle.DELETED,
                        )
                    )
        return history

    def events(self) -> Sequence[ClusterEvent]:
        return events_from_bodies(self.event_bodies)

    def error_logs(self) -> Sequence[LogRecord]:
        return self.error_items

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure]:
        # The live stack does not scrape cAdvisor yet; container status still covers OOM kills.
        return []

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        return [
            {
                "timestamp": r.at.isoformat() if r.at else None,
                "severity": r.severity,
                "body": r.message,
            }
            for r in self.error_items
            if r.service == service
        ][:limit]


def incident_window(alerts: Sequence[Alert], ends_at: datetime) -> tuple[datetime, datetime]:
    """Journal lookback of two hours before the first alert, through ``ends_at``.

    Pass the current time for an open incident so fresh evidence keeps landing
    in the window; pass a resolution time for a closed one so the window stops
    growing and later, unrelated changes cannot become causal candidates.
    """
    starts = min((a.starts_at for a in alerts), default=ends_at)
    return starts - timedelta(hours=2), ends_at


__all__ = [
    "ChangeWatcher",
    "ClusterReader",
    "KubernetesClusterReader",
    "LiveSource",
    "LogReader",
    "LokiLogReader",
    "events_from_bodies",
    "incident_window",
]
