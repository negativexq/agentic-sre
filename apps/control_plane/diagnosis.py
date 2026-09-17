"""Runs the diagnosis engine for stored incidents against the live cluster."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from packages.rca.engine import Investigator, diagnose
from packages.rca.live import (
    ChangeWatcher,
    ClusterReader,
    KubernetesClusterReader,
    LiveSource,
    LogReader,
    LokiLogReader,
    incident_window,
)
from packages.rca.llm import LLMClient
from packages.rca.model import Alert, Diagnosis
from packages.storage import (
    AlertRepository,
    DiagnosisRepository,
    EventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    ObjectVersionRepository,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"RESOLVED", "CLOSED", "FAILED"})


@dataclass
class DiagnosisService:
    """Glue between storage, cluster readers, and the engine."""

    session_factory: sessionmaker[Session]
    namespaces: tuple[str, ...]
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

    def snapshot(self) -> int:
        """Record changed objects, deletions, and new events; how many were stored."""
        if self.reader is None:
            return 0
        with self._snapshot_lock, self.session_factory() as session:
            repository = ObjectVersionRepository(session)
            watcher = ChangeWatcher(
                self.reader,
                self.namespaces,
                repository.record,
                lambda: repository.live_keys(set(self.namespaces)),
                repository.tombstone,
                self.clock,
            )
            stored = watcher.snapshot()
            now = self.clock()
            events = EventRepository(session)
            try:
                event_bodies = self.reader.list_events(self.namespaces)
            except Exception:
                # Object journaling has already committed its own facts. A
                # transient Event-list failure is missing evidence, not a
                # reason to roll back or synthesize object lifecycle state.
                logger.warning(
                    "event snapshot failed; continuing without new Events", exc_info=True
                )
                event_bodies = []
            stored += sum(events.record(body, now) for body in event_bodies)
            return stored

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
        now = self.clock()
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
        window_end = incident.updated_at if resolved else now
        current = []
        if self.reader is not None:
            # snapshot() also journals events, so both incidents read a
            # consistent, replayable history instead of the cluster's live,
            # garbage-collected event list.
            self.snapshot()
            if not resolved:
                current = self.reader.list_objects(self.namespaces)
        starts_at, ends_at = incident_window(alerts, window_end)
        with self.session_factory() as session:
            journal = ObjectVersionRepository(session).history(
                namespaces=set(self.namespaces), starts_at=starts_at, ends_at=ends_at
            )
            event_bodies = EventRepository(session).analysis_view(
                namespaces=set(self.namespaces), starts_at=starts_at, ends_at=ends_at
            )
        logs = []
        if self.log_reader is not None:
            try:
                services = sorted(
                    {
                        str(body.get("metadata", {}).get("name"))
                        for body in current
                        if body.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}
                    }
                    | {alert.service for alert in alerts if alert.service}
                )
                logs = self.log_reader.error_logs(services, starts_at, ends_at)
            except Exception:
                logger.warning("log backend unavailable; diagnosing without logs", exc_info=True)
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
            DiagnosisRepository(session).save(incident_id, diagnosis.model_dump(mode="json"), now)
        return diagnosis


def service_from_environment(session_factory: sessionmaker[Session]) -> DiagnosisService:
    """Configure cluster access from environment variables; offline by default."""
    namespaces = tuple(
        item.strip()
        for item in os.getenv("SRE_WATCH_NAMESPACES", "sre-demo").split(",")
        if item.strip()
    )
    reader = KubernetesClusterReader() if os.getenv("SRE_CLUSTER_ACCESS") == "true" else None
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
        reader=reader,
        log_reader=log_reader,
        investigator_factory=investigator,
    )


__all__ = ["DiagnosisService", "service_from_environment"]
