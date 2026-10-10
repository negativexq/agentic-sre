"""Live cluster access: read-only object listing, change journal, and incident source."""

from __future__ import annotations

import importlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from packages.rca.alert_coverage import AlertCoverageBoundary
from packages.rca.evidence_coverage import EvidenceCoverage
from packages.rca.investigation.environment import (
    InvestigationBackend,
    PrometheusInvestigationBackend,
    SourceInvestigationBackend,
    TempoInvestigationBackend,
)
from packages.rca.json_access import child, object_content_hash
from packages.rca.model import (
    CLUSTER_SCOPE,
    Alert,
    ClusterEvent,
    EntityRef,
    InvestigationQuery,
    JournalEntry,
    Lifecycle,
    LogRecord,
    ObjectVersion,
    PodStatusObservation,
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
    object_key,
    snapshot_evidence_id,
)
from packages.rca.pod_status import LifecycleStatusRecord, pod_status_from_lifecycle
from packages.rca.provider_adapter import (
    ProviderBoundary,
    ProviderReadPersistenceError,
)

_log = logging.getLogger(__name__)
# Resource reads start this long before the requested time so a baseline exists.
RESOURCE_BASELINE_LEAD = timedelta(minutes=5)
PROMETHEUS_MAX_QUERY_SPAN = timedelta(hours=1)

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
_NAMESPACED_METHODS = {kind: (group, method) for group, method, kind in _NAMESPACED_LISTS}
_CHAOS_KINDS = dict(_CHAOS_PLURALS)
_LOG_ERRORS = "(?i)(error|exception|fatal|refused|timeout|unavailable|unreachable)"


WatchEventType = Literal["ADDED", "MODIFIED", "DELETED", "BOOKMARK"]


def keep_write_times(metadata: dict[str, Any]) -> None:
    """Keep each ``managedFields`` entry's manager, operation and time; drop the field sets (m21 §26 A).

    The times are the API server's record of when each manager last wrote the object.
    """
    entries = metadata.pop("managedFields", None)
    if not isinstance(entries, list):
        return
    kept = [
        {key: entry[key] for key in ("manager", "operation", "time") if key in entry}
        for entry in entries
        if isinstance(entry, dict)
    ]
    if kept:
        metadata["managedFields"] = kept


@dataclass(frozen=True)
class WatchEvent:
    """One event of a scope's watch (connector contract §15)."""

    type: WatchEventType
    body: dict[str, Any]
    resource_version: str


class ResourceVersionExpired(Exception):
    """The watch cannot resume from its version: continuity is lost (``410 Gone``)."""


class ClusterReader(Protocol):
    """Read-only view of the cluster."""

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing: ...

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]: ...


class LogReader(Protocol):
    def error_logs(
        self, services: Sequence[str], starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord] | ProviderReadFailure: ...


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
    # The resourceVersion each scope's LIST returned, for a watch to start from (connector contract §15).
    resource_versions: Mapping[ListingScope, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectSnapshot:
    """One object-journal cycle and the boundary at which it was captured."""

    listing: ObjectListing
    stored_versions: int
    started_at: datetime
    completed_at: datetime
    # When the listing was taken: the time journal versions of this cycle carry.
    observed_at: datetime | None = None


class KubernetesClusterReader:
    """Lists objects and events with the Kubernetes Python client (read verbs only)."""

    def __init__(self, *, chaos_namespaces: Sequence[str] = ("chaos-mesh",)) -> None:
        self.chaos_namespaces = tuple(chaos_namespaces)
        self._skipped_chaos_scopes: set[ListingScope] = set()
        self._module: Any | None = None
        self._api_client: Any | None = None
        # the resourceVersion each Event scope's last LIST returned (connector contract §15)
        self.event_resource_versions: dict[ListingScope, str] = {}
        # API requests this reader made, by verb (connector contract §15 load measurement)
        self.api_calls: dict[str, int] = defaultdict(int)

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
        keep_write_times(child(body, "metadata"))
        return body

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        kubernetes, _api_client = self._client()
        objects: list[dict[str, Any]] = []
        completed: set[ListingScope] = set()
        failures: list[ListingFailure] = []
        workload_namespaces = [
            namespace for namespace in namespaces if namespace not in self.chaos_namespaces
        ]
        versions: dict[ListingScope, str] = {}
        for group, method, kind in _NAMESPACED_LISTS:
            for namespace in workload_namespaces:
                scope = ListingScope(namespace, kind)
                try:
                    api = getattr(kubernetes.client, group)()
                    self.api_calls["list"] += 1
                    response = getattr(api, method)(namespace)
                    items = response.items
                except Exception as exc:
                    failures.append(ListingFailure(scope, f"{type(exc).__name__}: {exc}"))
                    continue
                completed.add(scope)
                version = getattr(getattr(response, "metadata", None), "resource_version", None)
                if version:
                    versions[scope] = str(version)
                objects.extend(self._serialize(item, kind, "v1") for item in items)
        custom = kubernetes.client.CustomObjectsApi()
        # Experiments live beside their targets as well as in the chaos namespaces
        # (chaos-objects-design.md §2).
        for namespace in dict.fromkeys((*namespaces, *self.chaos_namespaces)):
            for kind, plural in _CHAOS_PLURALS:
                scope = ListingScope(namespace, kind)
                try:
                    self.api_calls["list"] += 1
                    listing = custom.list_namespaced_custom_object(
                        "chaos-mesh.org", "v1alpha1", namespace, plural
                    )
                except Exception as exc:  # CRD not installed or not readable
                    status = getattr(exc, "status", None)
                    if namespace not in self.chaos_namespaces and status in (403, 404):
                        # Optional here: no Chaos Mesh (404) or no permission to read it (403). Skipped,
                        # not failed, so it never makes the listing incomplete (measured: a 403 kept the
                        # Connector relisting forever); said once per scope.
                        if scope not in self._skipped_chaos_scopes:
                            self._skipped_chaos_scopes.add(scope)
                            _log.warning(
                                "chaos experiments in %s are not listed (%s %s)",
                                namespace,
                                status,
                                type(exc).__name__,
                            )
                        continue
                    failures.append(ListingFailure(scope, f"{type(exc).__name__}: {exc}"))
                    continue
                completed.add(scope)
                version = (listing.get("metadata") or {}).get("resourceVersion")
                if version:
                    versions[scope] = str(version)
                for item in listing.get("items", []):
                    item["kind"] = kind
                    keep_write_times(child(item, "metadata"))
                    objects.append(item)
        return ObjectListing(
            tuple(objects), frozenset(completed), tuple(failures), resource_versions=versions
        )

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        kubernetes, _api_client = self._client()
        core = kubernetes.client.CoreV1Api()
        events: list[dict[str, Any]] = []
        for namespace in dict.fromkeys((*namespaces, *self.chaos_namespaces)):
            try:
                self.api_calls["list"] += 1
                response = core.list_namespaced_event(namespace)
            except Exception:
                continue
            version = getattr(getattr(response, "metadata", None), "resource_version", None)
            if version:
                self.event_resource_versions[ListingScope(namespace, "Event")] = str(version)
            events.extend(self._serialize(item, "Event", "v1") for item in response.items)
        return events

    def list_scope(self, scope: ListingScope) -> tuple[list[dict[str, Any]], str]:
        """LIST one scope: its current items, serialized as ``list_objects`` does, and its version.

        Used to relist only the scope whose watch version expired (connector contract §15.4).
        """
        kubernetes, _api_client = self._client()
        self.api_calls["list"] += 1
        if scope.kind == "Event":
            response = kubernetes.client.CoreV1Api().list_namespaced_event(scope.namespace)
            items = [self._serialize(item, "Event", "v1") for item in response.items]
            return items, str(response.metadata.resource_version)
        if scope.kind in _CHAOS_KINDS:
            listing = kubernetes.client.CustomObjectsApi().list_namespaced_custom_object(
                "chaos-mesh.org", "v1alpha1", scope.namespace, _CHAOS_KINDS[scope.kind]
            )
            items = []
            for item in listing.get("items", []):
                item["kind"] = scope.kind
                keep_write_times(child(item, "metadata"))
                items.append(item)
            return items, str((listing.get("metadata") or {}).get("resourceVersion"))
        group, method = _NAMESPACED_METHODS[scope.kind]
        response = getattr(getattr(kubernetes.client, group)(), method)(scope.namespace)
        items = [self._serialize(item, scope.kind, "v1") for item in response.items]
        return items, str(response.metadata.resource_version)

    def scope_version(self, scope: ListingScope) -> str:
        """The current resource version of one scope from a consistent ``limit=1`` LIST (contract §15.6)."""
        kubernetes, _api_client = self._client()
        if scope.kind == "Event":
            response: Any = kubernetes.client.CoreV1Api().list_namespaced_event(
                scope.namespace, limit=1
            )
        elif scope.kind in _CHAOS_KINDS:
            response = kubernetes.client.CustomObjectsApi().list_namespaced_custom_object(
                "chaos-mesh.org", "v1alpha1", scope.namespace, _CHAOS_KINDS[scope.kind], limit=1
            )
        else:
            group, method = _NAMESPACED_METHODS[scope.kind]
            response = getattr(getattr(kubernetes.client, group)(), method)(
                scope.namespace, limit=1
            )
        self.api_calls["bookmark"] += 1
        metadata = response.get("metadata", {}) if isinstance(response, dict) else response.metadata
        version = (
            metadata.get("resourceVersion")
            if isinstance(metadata, dict)
            else getattr(metadata, "resource_version", None)
        )
        if not version:
            raise RuntimeError(f"no resource version for {scope.namespace}/{scope.kind}")
        return str(version)

    def watch(
        self, scope: ListingScope, resource_version: str, *, timeout_seconds: int = 300
    ) -> Iterator[WatchEvent]:
        """Watch one scope from ``resource_version`` until the server ends the watch (contract §15).

        Bodies are serialized exactly as a LIST serializes them, so an unchanged object has the same
        digest either way. An expired version raises ``ResourceVersionExpired``.
        """
        kubernetes, _api_client = self._client()
        watch_module = importlib.import_module("kubernetes.watch")
        if scope.kind == "Event":
            func: Any = kubernetes.client.CoreV1Api().list_namespaced_event
            args: tuple[Any, ...] = (scope.namespace,)
        elif scope.kind in _CHAOS_KINDS:
            func = kubernetes.client.CustomObjectsApi().list_namespaced_custom_object
            args = ("chaos-mesh.org", "v1alpha1", scope.namespace, _CHAOS_KINDS[scope.kind])
        else:
            group, method = _NAMESPACED_METHODS[scope.kind]
            func = getattr(getattr(kubernetes.client, group)(), method)
            args = (scope.namespace,)
        watcher = watch_module.Watch()
        self.api_calls["watch"] += 1
        try:
            for raw in watcher.stream(
                func,
                *args,
                resource_version=resource_version,
                allow_watch_bookmarks=True,
                timeout_seconds=timeout_seconds,
            ):
                kind_of_event = str(raw.get("type"))
                item = raw.get("object")
                if kind_of_event == "ERROR":
                    code = item.get("code") if isinstance(item, dict) else None
                    if code == 410:
                        raise ResourceVersionExpired(str(item.get("message", "410 Gone")))
                    raise RuntimeError(f"watch error: {item}")
                if isinstance(item, dict):
                    body = item
                    body["kind"] = scope.kind
                    keep_write_times(child(body, "metadata"))
                else:
                    body = self._serialize(item, scope.kind, "v1")
                version = str(child(body, "metadata").get("resourceVersion") or resource_version)
                yield WatchEvent(type=kind_of_event, body=body, resource_version=version)  # type: ignore[arg-type]
        except ResourceVersionExpired:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 410:
                raise ResourceVersionExpired(str(exc)) from exc
            raise
        finally:
            watcher.stop()


@dataclass
class LokiLogReader:
    """Warning and error lines from Loki for the incident window."""

    base_url: str
    timeout_seconds: float = 5.0
    limit: int = 500
    opener: Callable[..., Any] = urlopen

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("Loki timeout must be numeric")
        if not 0.1 <= float(self.timeout_seconds) <= 30.0:
            raise ValueError("Loki timeout must be between 0.1 and 30 seconds")
        self.timeout_seconds = float(self.timeout_seconds)
        if isinstance(self.limit, bool) or self.limit < 1:
            raise ValueError("Loki result limit must be positive")

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        if (
            starts_at.tzinfo is None
            or starts_at.utcoffset() is None
            or ends_at.tzinfo is None
            or ends_at.utcoffset() is None
            or ends_at < starts_at
            or ends_at - starts_at > LOKI_MAX_QUERY_SPAN
        ):
            raise ValueError("Loki query requires an ordered timezone-aware window <= 3600 seconds")
        # Preserve the existing incident-capture limit for legacy callers;
        # investigation queries explicitly pass their stricter <=32 bound.
        result_limit = self.limit if limit is None else min(self.limit, limit, 32)
        if result_limit < 1:
            raise ValueError("Loki result limit must be positive")
        names = sorted({name for name in services if re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)})
        if not names:
            return []
        selector = "|".join(name.replace(".", "\\.") for name in names)
        params = {
            "query": f'{{service_name=~"{selector}"}} |~ "{_LOG_ERRORS}"',
            "start": str(int(starts_at.timestamp() * 1e9)),
            "end": str(int(ends_at.timestamp() * 1e9)),
            "limit": str(result_limit),
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
        return records[:result_limit]


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


def events_from_bodies(
    bodies: Sequence[Mapping[str, Any]], evidence_ids: Sequence[str] | None = None
) -> list[ClusterEvent]:
    """Cluster Events from bodies; ``evidence_ids`` (one per body) name persisted versions.

    The product path always passes the persisted ``event:<version_pk>`` ids.
    Without them the Event's own UID, or its position, is used.
    """
    if evidence_ids is not None and len(evidence_ids) != len(bodies):
        raise ValueError("one evidence id per Event body is required")
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
                involved_uid=(
                    involved.get("uid") if isinstance(involved.get("uid"), str) else None
                ),
                reason=str(body.get("reason") or ""),
                type=str(body.get("type") or "Normal"),
                message=str(body.get("message") or "")[:500],
                first_at=first,
                last_at=_time(body.get("lastTimestamp")) or first,
                count=int(body.get("count") or 1),
                evidence_id=(
                    evidence_ids[index]
                    if evidence_ids is not None
                    else f"event:{child(body, 'metadata').get('uid') or index}"
                ),
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
        return ObjectSnapshot(listing, stored, started_at, completed_at, observed_at)


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
    provider_adapter: ProviderBoundary | None = None
    # Persisted lifecycle ledger rows for the incident window: the only source
    # of Pod status evidence.
    lifecycle_records: Sequence[LifecycleStatusRecord] = ()
    # The persisted snapshot cycle ``current_objects`` was loaded from. Current
    # objects are evidence only as members of a persisted cycle.
    snapshot_cycle_id: int | None = None
    snapshot_observed_at: datetime | None = None
    # The alert-channel coverage frozen at the run boundary (M21 contract §10.2).
    alert_coverage: AlertCoverageBoundary | None = None
    # The object channel's coverage frozen at the run boundary (m21 §26.6).
    evidence_coverage: EvidenceCoverage | None = None
    # Persisted ``event:<version_pk>`` ids, one per ``event_bodies`` item.
    event_evidence_ids: Sequence[str] | None = None
    # Spans captured for the incident and frozen by the run's manifest (live-trace-design.md §3).
    trace_items: Sequence[TraceSpanObservation] = ()

    def incident_id(self) -> str:
        return self.incident

    def observation_cutoff(self) -> datetime | None:
        """Return the diagnosis snapshot or frozen resolution boundary."""
        return self.observed_at

    def alerts(self) -> Sequence[Alert]:
        return self.alert_items

    def alert_observation_start(self) -> datetime | None:
        """W from the persisted boundary; unknown without contiguous coverage."""
        return self.alert_coverage.alert_observation_start if self.alert_coverage else None

    def evidence_coverage_record(self) -> EvidenceCoverage | None:
        return self.evidence_coverage

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        """Journal versions plus the run's persisted snapshot cycle.

        Snapshot objects enter as UPDATED versions with their persisted
        ``snapshot:<cycle>:<key>`` ids when their content differs from the
        journal. Absence from the snapshot never produces a deletion; only a
        persisted journal tombstone does. A resolved incident's frozen window
        (``current_is_live=False``) uses the journal alone.
        """
        history: dict[EntityRef, list[ObjectVersion]] = {}
        hashes: dict[EntityRef, str] = {}

        def add(
            body: dict[str, Any],
            at: datetime,
            evidence: str,
            lifecycle: Lifecycle,
            *,
            uid: str | None = None,
        ) -> None:
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
                    uid=uid,
                    observed_at=at,
                    body=body,
                    evidence_id=evidence,
                    lifecycle=lifecycle,
                )
            )

        for entry in self.journal:
            persisted_uid = child(entry.body, "metadata").get("uid")
            add(
                entry.body,
                entry.observed_at,
                f"journal:{entry.version_id}",
                entry.lifecycle,
                uid=persisted_uid if isinstance(persisted_uid, str) else None,
            )
        if self.current_is_live and self.snapshot_cycle_id is not None:
            at = self.snapshot_observed_at or self.observed_at
            for body in self.current_objects:
                key = object_key(body)
                if key is None:
                    continue
                uid = child(body, "metadata").get("uid")
                add(
                    body,
                    at,
                    snapshot_evidence_id(self.snapshot_cycle_id, key),
                    Lifecycle.UPDATED,
                    uid=uid if isinstance(uid, str) else None,
                )
        return history

    def events(self) -> Sequence[ClusterEvent]:
        return events_from_bodies(self.event_bodies, self.event_evidence_ids)

    def error_logs(self) -> Sequence[LogRecord]:
        return self.error_items

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure] | ProviderReadFailure:
        """Bounded per-Pod reads from ``since`` minus the baseline gap up to the cutoff.

        Each read stays within the one-hour query bound. Failures stay explicit
        so partial/absent provider data cannot be read as normal evidence.
        """
        if self.provider_adapter is None or not self.provider_adapter.supports("resource_pressure"):
            return []
        start = since - RESOURCE_BASELINE_LEAD
        end = min(self.observed_at, start + PROMETHEUS_MAX_QUERY_SPAN)
        if end <= start:
            return []
        records: list[ResourcePressure] = []
        failures: list[ProviderReadFailure] = []
        for pod in pods:
            if pod.kind != "Pod":
                continue
            result = self.provider_adapter.query_resource_pressure(
                pod, InvestigationQuery(start=start, end=end, limit=16)
            )
            if isinstance(result, ProviderReadFailure):
                failures.append(result)
            else:
                records.extend(result)
        return failures[0] if failures else records

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        # The live stack does not yet expose a bounded request-rate reader.
        return []

    def trace_observations(self) -> Sequence[TraceSpanObservation]:
        """The spans of the run's manifest, exactly as captured (live-trace-design.md §3)."""
        return list(self.trace_items)

    def pod_status_observations(self) -> Sequence[PodStatusObservation]:
        """Pod status exactly as the lifecycle ledger recorded it.

        Neither journal bodies nor the current listing are read for status:
        every observation is one persisted STATUS_SNAPSHOT or READY_* row.
        """
        return pod_status_from_lifecycle(self.lifecycle_records)

    def supports(self, capability: str) -> bool:
        if capability == "incident_events":
            return self.supports("events")
        if capability == "incident_changes":
            return self.supports("history")
        if capability == "runtime_traces":
            return self.provider_adapter is not None and self.provider_adapter.supports(capability)
        if capability in {"resource_pressure", "traffic"}:
            return self.provider_adapter is not None and self.provider_adapter.supports(capability)
        return capability in {"history", "events", "logs"}

    def supports_typed_runtime(self, capability: str) -> bool:
        """Report configured typed providers independently of query results."""
        if capability == "runtime_traces":
            return self.provider_adapter is not None and self.provider_adapter.supports(capability)
        if capability in {"resource_pressure", "traffic"}:
            return self.provider_adapter is not None and self.provider_adapter.supports(capability)
        if capability == "logs":
            return self.provider_adapter is not None and self.provider_adapter.supports(capability)
        return False

    def investigation_backend(self) -> InvestigationBackend:
        base: InvestigationBackend = SourceInvestigationBackend(self)
        if self.provider_adapter is None:
            return base
        investigation_adapter = self.provider_adapter.for_caller("INVESTIGATION")
        if investigation_adapter.supports("resource_pressure"):
            base = PrometheusInvestigationBackend(
                base=base,
                provider_adapter=investigation_adapter,
                observation_cutoff=self.observation_cutoff(),
            )
        if investigation_adapter.supports("runtime_traces"):
            base = TempoInvestigationBackend(
                base=base,
                provider_adapter=investigation_adapter,
                observation_cutoff=self.observation_cutoff(),
            )
        if investigation_adapter.supports("logs"):
            from packages.rca.investigation.environment import LokiInvestigationBackend

            base = LokiInvestigationBackend(
                base=base,
                provider_adapter=investigation_adapter,
                source=self,
                observation_cutoff=self.observation_cutoff(),
            )
        return base

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


LOKI_MAX_QUERY_SPAN = timedelta(hours=1)
LOG_CAPTURE_MAX_SLICES = 3
LOG_CAPTURE_MAX_RECORDS = 500


def bounded_slices(
    starts_at: datetime, ends_at: datetime, span: timedelta = LOKI_MAX_QUERY_SPAN
) -> tuple[tuple[datetime, datetime], ...]:
    """Split ``[starts_at, ends_at)`` into contiguous slices of at most ``span``, newest first.

    Loki's ``query_range`` returns entries with ``start <= ts < end``, so
    contiguous slices partition the window without overlap. An empty window
    yields no slice.
    """
    if ends_at < starts_at:
        raise ValueError("capture window must be ordered")
    if span <= timedelta(0):
        raise ValueError("slice span must be positive")
    slices: list[tuple[datetime, datetime]] = []
    end = ends_at
    while end > starts_at:
        start = max(starts_at, end - span)
        slices.append((start, end))
        end = start
    return tuple(slices)


TRACE_CAPTURE_MAX_SERVICES = 8
TRACE_CAPTURE_MAX_SPANS = 500
TRACE_QUERY_MAX_SPAN = timedelta(hours=1)  # the Tempo reader's bound


def trace_slices(
    onset: datetime, end: datetime, *, lead: timedelta, width: timedelta
) -> list[tuple[datetime, datetime]]:
    """``[onset - lead, end]`` cut on a grid anchored at ``onset`` (live-trace-design.md §12.2).

    Each slice is one search, so every slice contributes its own sample; only the last one may be partial. Tempo takes
    whole seconds, so a last slice within one whole second cannot be read and is left out (§12.7); a later capture
    reads that part with its own, longer last slice.
    """
    slices: list[tuple[datetime, datetime]] = []
    start = onset - lead
    while start < end:
        stop = min(start + width, end)
        if int(start.timestamp()) != int(stop.timestamp()):
            slices.append((start, stop))
        start += width
    return slices


@dataclass(frozen=True)
class TraceServiceCapture:
    """One service's bounded trace read: how complete it was, never repaired."""

    service: str
    namespace: str
    starts_at: datetime
    ends_at: datetime
    completeness: str  # BEST_EFFORT, TRUNCATED or FAILED
    spans: int
    error: str = ""


@dataclass(frozen=True)
class TraceCapture:
    """Spans read for one incident (live-trace-design.md §3); duplicates across services removed."""

    spans: tuple[TraceSpanObservation, ...]
    reads: tuple[TraceServiceCapture, ...]


def capture_traces(
    reader: Any,
    services: Sequence[tuple[str, str]],
    starts_at: datetime,
    ends_at: datetime,
    *,
    max_services: int = TRACE_CAPTURE_MAX_SERVICES,
    max_spans: int = TRACE_CAPTURE_MAX_SPANS,
) -> TraceCapture:
    """One bounded Tempo read per ``(namespace, service)`` Deployment over the newest hour of the window.

    A read that failed or was cut is recorded as such; it is coverage, never absence of spans.
    """
    from packages.rca.investigation.tempo import TempoTraceBatch
    from packages.rca.model import InvestigationQuery, ProviderReadFailure

    start = max(starts_at, ends_at - TRACE_QUERY_MAX_SPAN)

    def read(
        namespace: str, service: str
    ) -> tuple[TraceServiceCapture, list[TraceSpanObservation]]:
        target = EntityRef(kind="Deployment", name=service, namespace=namespace)
        try:
            result = reader.query_tempo(
                target, InvestigationQuery(start=start, end=ends_at, limit=32)
            )
        except Exception as error:  # noqa: BLE001 - a failed read is recorded, never fatal
            return (
                TraceServiceCapture(
                    service, namespace, start, ends_at, "FAILED", 0, type(error).__name__
                ),
                [],
            )
        if isinstance(result, ProviderReadFailure):
            return (
                TraceServiceCapture(
                    service, namespace, start, ends_at, "FAILED", 0, result.error_type
                ),
                [],
            )
        spans = result.spans if isinstance(result, TempoTraceBatch) else tuple(result)
        completeness = (
            str(result.diagnostics.completeness.value)
            if isinstance(result, TempoTraceBatch)
            else "BEST_EFFORT"
        )
        ordered = sorted(spans, key=lambda span: (span.start_at, span.trace_id, span.span_id))
        if len(ordered) > max_spans:
            ordered, completeness = ordered[:max_spans], "TRUNCATED"
        return (
            TraceServiceCapture(service, namespace, start, ends_at, completeness, len(ordered)),
            ordered,
        )

    results = [
        read(namespace, service)
        for namespace, service in list(dict.fromkeys(services))[:max_services]
    ]
    seen: dict[tuple[str, str], TraceSpanObservation] = {}
    reads: list[TraceServiceCapture] = []
    for capture_read, ordered in results:
        for span in ordered:
            seen.setdefault((span.trace_id, span.span_id), span)
        reads.append(capture_read)
    spans_out = tuple(
        sorted(seen.values(), key=lambda span: (span.start_at, span.trace_id, span.span_id))
    )
    return TraceCapture(spans_out, tuple(reads))


@dataclass(frozen=True)
class LogSliceFailure:
    """One bounded capture slice whose read failed, with the error that caused it."""

    starts_at: datetime
    ends_at: datetime
    error_type: str
    error: str


@dataclass(frozen=True)
class LogCapture:
    """The outcome of capturing incident error logs through bounded reads."""

    records: tuple[LogRecord, ...]
    source_read_ids: tuple[int | None, ...]
    queried: tuple[tuple[datetime, datetime], ...]
    failed: tuple[LogSliceFailure, ...]
    skipped: tuple[tuple[datetime, datetime], ...]

    @property
    def succeeded(self) -> bool:
        """At least one bounded read returned (possibly empty) data."""
        return len(self.queried) > len(self.failed)


def capture_error_logs(
    reader: LogReader,
    services: Sequence[str],
    starts_at: datetime,
    ends_at: datetime,
    *,
    max_slices: int = LOG_CAPTURE_MAX_SLICES,
    max_records: int = LOG_CAPTURE_MAX_RECORDS,
) -> LogCapture:
    """Capture an incident window as several bounded reads instead of one wide one.

    The incident history (two hours before the first alert, through now) is
    wider than a single bounded Loki read allows, so it is read as contiguous
    slices of at most one hour, newest first — the same recency a single
    backward query had. Reading stops at ``max_records`` or ``max_slices``;
    anything not read is reported in ``skipped`` rather than dropped silently,
    and earlier captures persisted for the incident still cover older slices.
    A failed slice is recorded with its error and does not discard the others.
    """
    if ends_at <= starts_at:
        # Nothing to read (or a skewed window); logs never fail a diagnosis.
        return LogCapture(records=(), source_read_ids=(), queried=(), failed=(), skipped=())
    slices = bounded_slices(starts_at, ends_at)
    records: list[LogRecord] = []
    source_read_ids: list[int | None] = []
    seen: set[tuple[str, datetime | None, str, str, str]] = set()
    queried: list[tuple[datetime, datetime]] = []
    failed: list[LogSliceFailure] = []
    skipped: tuple[tuple[datetime, datetime], ...] = ()
    for index, (start, end) in enumerate(slices):
        if index >= max_slices or len(records) >= max_records:
            skipped = slices[index:]
            break
        queried.append((start, end))
        try:
            read_with_id = getattr(reader, "error_logs_with_read_id", None)
            if callable(read_with_id):
                read_result = read_with_id(services, start, end)
                if isinstance(read_result, ProviderReadFailure):
                    failed.append(
                        LogSliceFailure(
                            start, end, read_result.error_type, read_result.error_message
                        )
                    )
                    continue
                batch, read_id = read_result
            else:
                read_result = reader.error_logs(services, start, end)
                if isinstance(read_result, ProviderReadFailure):
                    failed.append(
                        LogSliceFailure(
                            start, end, read_result.error_type, read_result.error_message
                        )
                    )
                    continue
                batch, read_id = read_result, None
        except ProviderReadPersistenceError:
            # A successful provider response whose tape commit failed must not
            # be treated like an ignorable provider slice failure.
            raise
        except Exception as error:  # noqa: BLE001 - one failed slice must not drop the rest
            failed.append(LogSliceFailure(start, end, type(error).__name__, str(error)[:200]))
            continue
        for record in sorted(batch, key=lambda item: item.at or start, reverse=True):
            key = (record.service, record.at, record.severity, record.message, record.evidence_id)
            if key not in seen:
                seen.add(key)
                records.append(record)
                source_read_ids.append(read_id)
    return LogCapture(
        records=tuple(records[:max_records]),
        source_read_ids=tuple(source_read_ids[:max_records]),
        queried=tuple(queried),
        failed=tuple(failed),
        skipped=skipped,
    )


def incident_window(alerts: Sequence[Alert], ends_at: datetime) -> tuple[datetime, datetime]:
    """Journal lookback of two hours before the first alert, through ``ends_at``.

    Pass the current time for an open incident so fresh evidence keeps landing
    in the window; pass a resolution time for a closed one so the window stops
    growing and later, unrelated changes cannot become causal candidates.
    """
    starts = min((a.starts_at for a in alerts), default=ends_at)
    return starts - timedelta(hours=2), ends_at


__all__ = [
    "LOG_CAPTURE_MAX_RECORDS",
    "LOG_CAPTURE_MAX_SLICES",
    "LOKI_MAX_QUERY_SPAN",
    "ChangeWatcher",
    "ClusterReader",
    "KubernetesClusterReader",
    "LiveSource",
    "LogCapture",
    "LogReader",
    "LogSliceFailure",
    "LokiLogReader",
    "bounded_slices",
    "capture_error_logs",
    "events_from_bodies",
    "incident_window",
]
