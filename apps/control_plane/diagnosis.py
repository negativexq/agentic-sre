"""Runs the diagnosis engine for stored incidents against the live cluster."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from packages.rca.engine import Investigator, diagnose
from packages.rca.live import (
    ChangeWatcher,
    ClusterReader,
    KubernetesClusterReader,
    ListingFailure,
    LiveSource,
    LogReader,
    LokiLogReader,
    ObjectSnapshot,
    incident_window,
)
from packages.rca.llm import LLMClient
from packages.rca.model import Alert, Diagnosis, LogRecord
from packages.storage import (
    AlertRepository,
    DiagnosisRepository,
    EventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    LogObservationRepository,
    ObjectVersionRepository,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"RESOLVED", "CLOSED", "FAILED"})


@dataclass(frozen=True)
class SnapshotResult:
    """Evidence captured by one diagnosis/watch snapshot cycle."""

    stored_versions: int
    started_at: datetime
    completed_at: datetime
    objects: tuple[dict[str, Any], ...] = ()
    event_bodies: tuple[dict[str, Any], ...] = ()
    failed_scopes: tuple[ListingFailure, ...] = ()
    event_listing_failed: bool = False


@dataclass
class DiagnosisService:
    """Glue between storage, cluster readers, and the engine."""

    session_factory: sessionmaker[Session]
    namespaces: tuple[str, ...]
    evidence_namespaces: tuple[str, ...] = ("chaos-mesh",)
    reader: ClusterReader | None = None
    log_reader: LogReader | None = None
    investigator_factory: Callable[[], Investigator | None] = lambda: None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Serializes ChangeWatcher.snapshot() runs: the periodic watch() loop and a
    # run()-triggered snapshot can otherwise race on the same read-then-write
    # (latest version, then insert) in ObjectVersionRepository.
    #
    # This is a process-local lock: it only protects a single DiagnosisService
    # instance. Today's deployment runs exactly one (one uvicorn worker, one
    # control-plane replica -- see infra/kubernetes/control-plane.yaml), so it
    # is sufficient. It stops being sufficient the moment more than one worker
    # or replica writes to the same database; that needs a database-level
    # lock (e.g. a Postgres advisory lock) instead, or a single writer process.
    _snapshot_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_snapshot_result: SnapshotResult | None = field(default=None, init=False, repr=False)

    @property
    def last_snapshot_result(self) -> SnapshotResult | None:
        """Return the latest cycle metadata, including incomplete scopes."""
        return self._last_snapshot_result

    def snapshot(self) -> int:
        """Record changed objects, deletions, and new events; how many were stored."""
        return self.snapshot_result().stored_versions

    def snapshot_result(self) -> SnapshotResult:
        """Capture one coherent object/Event cycle with an explicit boundary."""
        if self.reader is None:
            now = self.clock()
            result = SnapshotResult(0, now, now)
            self._last_snapshot_result = result
            return result
        journal_namespaces = tuple(dict.fromkeys((*self.namespaces, *self.evidence_namespaces)))
        with self._snapshot_lock, self.session_factory() as session:
            repository = ObjectVersionRepository(session)
            watcher = ChangeWatcher(
                self.reader,
                journal_namespaces,
                repository.record,
                lambda: repository.live_keys(set(journal_namespaces)),
                repository.tombstone,
                self.clock,
            )
            object_snapshot: ObjectSnapshot = watcher.snapshot_result()
            event_observed_at = self.clock()
            events = EventRepository(session)
            event_listing_failed = False
            try:
                event_bodies = self.reader.list_events(journal_namespaces)
            except Exception:
                # Object journaling has already committed its own facts. A
                # transient Event-list failure is missing evidence, not a
                # reason to roll back or synthesize object lifecycle state.
                logger.warning(
                    "event snapshot failed; continuing without new Events", exc_info=True
                )
                event_bodies = []
                event_listing_failed = True
            stored = object_snapshot.stored_versions + sum(
                events.record(body, event_observed_at) for body in event_bodies
            )
            result = SnapshotResult(
                stored_versions=stored,
                started_at=object_snapshot.started_at,
                completed_at=self.clock(),
                objects=object_snapshot.listing.objects,
                event_bodies=tuple(event_bodies),
                failed_scopes=object_snapshot.listing.failed_scopes,
                event_listing_failed=event_listing_failed,
            )
            self._last_snapshot_result = result
            if result.failed_scopes:
                logger.warning(
                    "cluster snapshot incomplete for scopes: %s",
                    ", ".join(
                        f"{failure.scope.namespace}/{failure.scope.kind} ({failure.error})"
                        for failure in result.failed_scopes
                    ),
                )
            return result

    def watch(self, stop: threading.Event, interval_seconds: float) -> None:
        """Snapshot the cluster until ``stop`` is set; errors are logged and retried."""
        while not stop.is_set():
            try:
                stored = self.snapshot()
                if stored:
                    logger.info("object journal stored %d changed object(s)", stored)
            except Exception:
                logger.warning("cluster snapshot failed", exc_info=True)
            stop.wait(interval_seconds)

    def run(self, incident_id: UUID) -> Diagnosis:
        with self.session_factory() as session:
            incident = IncidentRepository(session).get(incident_id)
            if incident is None:
                raise IncidentNotFoundError(str(incident_id))
            alerts = [
                Alert(
                    name=item.alert_name,
                    service=item.service,
                    namespace=item.namespace,
                    starts_at=item.starts_at,
                    labels={**item.labels, "service_name": item.service},
                )
                for item in AlertRepository(session).list_for_incident(incident_id)
            ]
        # Once resolved, the incident's causal window is frozen at its last
        # transition; otherwise it keeps growing to "now" so an open incident
        # keeps picking up fresh evidence. Freezing it also stops fetching a
        # live cluster snapshot for it, so unrelated changes made afterwards
        # cannot show up as its "latest" object version.
        resolved = incident.status in _TERMINAL_STATUSES
        window_end = incident.updated_at if resolved else self.clock()
        current: list[dict[str, Any]] = []
        if self.reader is not None:
            if not resolved:
                # The current object view is the same listing that was
                # journaled. Do not perform a second, later read whose data
                # would fall outside the advertised diagnosis cutoff.
                cycle = self.snapshot_result()
                window_end = cycle.completed_at
                current = list(cycle.objects)
            else:
                # Keep the shared journal current for later incidents, but do
                # not use this live cycle as evidence for the frozen episode.
                # A reader outage must not make historical diagnosis fail.
                try:
                    self.snapshot_result()
                except Exception:
                    logger.warning(
                        "post-resolution journal refresh failed; using frozen evidence",
                        exc_info=True,
                    )
        starts_at, ends_at = incident_window(alerts, window_end)
        journal_namespaces = set(self.namespaces) | set(self.evidence_namespaces)
        with self.session_factory() as session:
            journal = ObjectVersionRepository(session).history(
                namespaces=journal_namespaces, starts_at=starts_at, ends_at=ends_at
            )
            event_bodies = EventRepository(session).analysis_view(
                namespaces=journal_namespaces, starts_at=starts_at, ends_at=ends_at
            )
            logs = LogObservationRepository(session).list_for_incident(
                incident_id=incident_id, starts_at=starts_at, ends_at=ends_at
            )
        if self.log_reader is not None and not resolved:
            try:
                services = sorted(
                    {
                        str(body.get("metadata", {}).get("name"))
                        for body in current
                        if body.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}
                    }
                    | {alert.service for alert in alerts if alert.service}
                )
                fetched_logs = self.log_reader.error_logs(services, starts_at, ends_at)
                # Loki records are queried for the cycle's bounded window. The
                # collection itself may finish a little later, so extend the
                # open diagnosis boundary to the end of that intentional
                # capture rather than dropping its observations on replay.
                log_observed_at = self.clock()
                with self.session_factory() as session:
                    LogObservationRepository(session).record(
                        incident_id, fetched_logs, log_observed_at
                    )
                logs = _merge_logs(logs, fetched_logs)
                window_end = max(window_end, log_observed_at)
            except Exception:
                logger.warning("log backend unavailable; diagnosing without logs", exc_info=True)
        starts_at, ends_at = incident_window(alerts, window_end)
        if not resolved:
            with self.session_factory() as session:
                logs = LogObservationRepository(session).list_for_incident(
                    incident_id=incident_id, starts_at=starts_at, ends_at=ends_at
                )
        source = LiveSource(
            incident=str(incident_id),
            alert_items=alerts,
            journal=journal,
            current_objects=current,
            event_bodies=event_bodies,
            error_items=logs,
            observed_at=window_end,
            current_is_live=not resolved,
        )
        diagnosis = diagnose(source, investigator=self.investigator_factory())
        with self.session_factory() as session:
            DiagnosisRepository(session).save(
                incident_id, diagnosis.model_dump(mode="json"), self.clock()
            )
        return diagnosis


def service_from_environment(session_factory: sessionmaker[Session]) -> DiagnosisService:
    """Configure cluster access from environment variables; offline by default."""
    namespaces = tuple(
        item.strip()
        for item in os.getenv("SRE_WATCH_NAMESPACES", "sre-demo").split(",")
        if item.strip()
    )
    evidence_namespaces = tuple(
        item.strip()
        for item in os.getenv("SRE_EVIDENCE_NAMESPACES", "chaos-mesh").split(",")
        if item.strip()
    )
    reader = (
        KubernetesClusterReader(chaos_namespaces=evidence_namespaces)
        if os.getenv("SRE_CLUSTER_ACCESS") == "true"
        else None
    )
    loki = os.getenv("SRE_LOKI_URL")
    log_reader = LokiLogReader(loki) if loki else None

    # Built once and reused: OpenAIClient owns the call-budget counter, so a
    # fresh client per incident would reset SRE_LLM_MAX_CALLS every time.
    llm_client: LLMClient | None = None

    def investigator() -> Investigator | None:
        nonlocal llm_client
        if os.getenv("SRE_LLM_ENABLED", "").casefold() != "true":
            return None
        if llm_client is None:
            from packages.rca.llm import OpenAIClient

            llm_client = OpenAIClient()
        from packages.rca.agent import LLMInvestigator

        return LLMInvestigator(llm_client)

    return DiagnosisService(
        session_factory=session_factory,
        namespaces=namespaces,
        evidence_namespaces=evidence_namespaces,
        reader=reader,
        log_reader=log_reader,
        investigator_factory=investigator,
    )


__all__ = ["DiagnosisService", "service_from_environment"]


def _merge_logs(existing: list[LogRecord], fetched: list[LogRecord]) -> list[LogRecord]:
    """Merge replayed and newly fetched records without duplicate evidence."""
    result: list[LogRecord] = []
    seen: set[tuple[str, datetime | None, str, str]] = set()
    for record in [*existing, *fetched]:
        key = (record.service, record.at, record.severity, record.message)
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result
