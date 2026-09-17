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
from packages.rca.model import Alert, Diagnosis
from packages.storage import (
    AlertRepository,
    DiagnosisRepository,
    IncidentNotFoundError,
    IncidentRepository,
    ObjectVersionRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class DiagnosisService:
    """Glue between storage, cluster readers, and the engine."""

    session_factory: sessionmaker[Session]
    namespaces: tuple[str, ...]
    reader: ClusterReader | None = None
    log_reader: LogReader | None = None
    investigator_factory: Callable[[], Investigator | None] = lambda: None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def snapshot(self) -> int:
        """Record changed and deleted objects in the journal; returns how many were stored."""
        if self.reader is None:
            return 0
        with self.session_factory() as session:
            repository = ObjectVersionRepository(session)
            watcher = ChangeWatcher(
                self.reader,
                self.namespaces,
                repository.record,
                lambda: repository.live_keys(set(self.namespaces)),
                repository.tombstone,
                self.clock,
            )
            return watcher.snapshot()

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
            if IncidentRepository(session).get(incident_id) is None:
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
        current, events = [], []
        if self.reader is not None:
            self.snapshot()
            current = self.reader.list_objects(self.namespaces)
            events = self.reader.list_events(self.namespaces)
        starts_at, ends_at = incident_window(alerts, now)
        with self.session_factory() as session:
            journal = ObjectVersionRepository(session).history(
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
            event_bodies=events,
            error_items=logs,
            observed_at=now,
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

    def investigator() -> Investigator | None:
        if os.getenv("SRE_LLM_ENABLED", "").casefold() != "true":
            return None
        from packages.rca.agent import LLMInvestigator
        from packages.rca.llm import OpenAIClient

        return LLMInvestigator(OpenAIClient())

    return DiagnosisService(
        session_factory=session_factory,
        namespaces=namespaces,
        reader=reader,
        log_reader=log_reader,
        investigator_factory=investigator,
    )


__all__ = ["DiagnosisService", "service_from_environment"]
