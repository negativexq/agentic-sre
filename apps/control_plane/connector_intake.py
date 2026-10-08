"""Control-plane intake of the connector's alert stream (connector contract §10).

The Connector delivers alert occurrences and its own polls of the alert channel; this consumer
turns them into incidents (through the unchanged idempotent ingestion) and into coverage segments
(through the unchanged segment rules). Delivery is at-least-once: a page is persisted before its
cursor is kept, and anything older than what is already recorded is skipped, so a replay after a
restart can neither duplicate an incident nor move coverage backwards.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from packages.connector import wire
from packages.connector.client import ConnectorClient, ConnectorError
from packages.contracts import Alert, AlertmanagerAlertPayload, AlertStatus
from packages.incident import IncidentManager, normalize_alert
from packages.rca.alert_coverage import ALERT_COVERAGE_SOURCE, AlertCoverageConfig
from packages.storage.models import AlertCoveragePollRow, AlertCoverageSegmentRow, AlertRow
from packages.storage.repositories import AlertCoverageRepository

logger = logging.getLogger(__name__)

# connector contract §17: the reason an occurrence that ended before the first coverage opens nothing
ENDED_BEFORE_FIRST_COVERAGE = "ENDED_BEFORE_FIRST_COVERAGE"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AlertStreamConsumer:
    def __init__(
        self,
        client: ConnectorClient,
        session_factory: sessionmaker[Session],
        *,
        config: AlertCoverageConfig | None = None,
        on_incidents: Callable[[list[UUID]], None] | None = None,
        on_refired: Callable[[list[UUID]], None] | None = None,
        on_resolved: Callable[[list[UUID]], None] | None = None,
        quiet: timedelta = timedelta(0),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_limit: int = wire.MAX_BATCH,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._config = config or AlertCoverageConfig()
        self._on_incidents = on_incidents
        self._on_refired = on_refired
        self._on_resolved = on_resolved
        self._quiet = quiet
        self._clock = clock
        self._limit = page_limit
        self.cursor: str | None = None
        self._available: bool | None = None  # log a change of state once, not every second
        self.ended_before_first_coverage = 0  # contract §17, for the coverage view and the tests

    def _latest_poll(self, session: Session) -> datetime | None:
        latest = session.scalar(
            select(func.max(AlertCoveragePollRow.completed_at)).where(
                AlertCoveragePollRow.source == ALERT_COVERAGE_SOURCE
            )
        )
        return _utc(latest) if latest is not None else None

    def _first_coverage(self, session: Session) -> datetime | None:
        """``W₀``: when this installation first observed the alert channel; None before it ever has."""
        first = session.scalar(
            select(func.min(AlertCoverageSegmentRow.started_at)).where(
                AlertCoverageSegmentRow.source == ALERT_COVERAGE_SOURCE
            )
        )
        return _utc(first) if first is not None else None

    def _before_first_coverage(self, session: Session, alert: Alert) -> bool:
        """Contract §17: a resolved occurrence, never recorded, whose whole active interval ended at or before
        ``W₀`` (or before any coverage at all) opens no incident. Later gaps are not considered."""
        if alert.status is not AlertStatus.RESOLVED or alert.ends_at is None:
            return False
        first = self._first_coverage(session)
        if first is not None and _utc(alert.ends_at) > first:
            return False
        known = session.scalar(
            select(AlertRow.alert_id).where(
                AlertRow.fingerprint == alert.fingerprint, AlertRow.starts_at == alert.starts_at
            )
        )
        return known is None

    def _connector_lost(self, error: ConnectorError) -> None:
        """A connector that cannot be reached observes nothing: close the open segment once."""
        now = self._clock()
        with self._session_factory() as session:
            repository = AlertCoverageRepository(session)
            if repository.open_segment(ALERT_COVERAGE_SOURCE) is not None:
                repository.record_failure(
                    source=ALERT_COVERAGE_SOURCE,
                    attempted_at=now,
                    completed_at=now,
                    error_type=type(error).__name__,
                )

    def step(self) -> int:
        """Read and apply one page; returns how many items and gaps it carried."""
        try:
            page = self._client.read("read_alerts", self.cursor, self._limit)
        except ConnectorError as error:
            if self._available is not False:
                logger.warning("alert stream unavailable: %s", error)
            self._available = False
            self._connector_lost(error)
            return 0
        if self._available is False:
            logger.info("alert stream available again")
        self._available = True
        incident_ids: list[UUID] = []
        refired_ids: list[UUID] = []
        resolved_ids: list[UUID] = []
        with self._session_factory() as session:
            repository = AlertCoverageRepository(session)
            recorded = self._latest_poll(session)
            for item in page.ordered():
                if isinstance(item, wire.AlertItem):
                    try:
                        payload = AlertmanagerAlertPayload.model_validate(item.alert)
                        alert = normalize_alert(payload)
                        if self._before_first_coverage(session, alert):
                            self.ended_before_first_coverage += 1
                            logger.info(
                                "alert %s (%s) not admitted: %s",
                                alert.alert_name,
                                alert.fingerprint,
                                ENDED_BEFORE_FIRST_COVERAGE,
                            )
                            continue
                        outcome = IncidentManager(session, quiet=self._quiet).ingest_occurrence(
                            alert, now=self._clock()
                        )
                    except ValueError:
                        logger.warning(
                            "skipping an alert the intake cannot normalize", exc_info=True
                        )
                        continue
                    # only a change of an incident starts a diagnosis (diagnosis-trigger-design.md §2)
                    if outcome.refired:
                        refired_ids.append(outcome.incident.incident_id)
                    elif outcome.created:
                        incident_ids.append(outcome.incident.incident_id)
                    resolved_ids.extend(outcome.resolved)
                elif isinstance(item, wire.HeartbeatItem):
                    if recorded is not None and _utc(item.completed_at) <= recorded:
                        continue
                    if item.ok:
                        repository.record_success(
                            source=ALERT_COVERAGE_SOURCE,
                            attempted_at=item.attempted_at,
                            completed_at=item.completed_at,
                            active_alerts=item.active_alerts or 0,
                            config=self._config,
                        )
                    else:
                        repository.record_failure(
                            source=ALERT_COVERAGE_SOURCE,
                            attempted_at=item.attempted_at,
                            completed_at=item.completed_at,
                            error_type=item.error_type or "ConnectorPollFailed",
                        )
                    recorded = _utc(item.completed_at)
                elif isinstance(item, wire.GapItem):
                    if recorded is not None and _utc(item.at) <= recorded:
                        continue
                    if repository.open_segment(ALERT_COVERAGE_SOURCE) is not None:
                        repository.record_failure(
                            source=ALERT_COVERAGE_SOURCE,
                            attempted_at=item.at,
                            completed_at=item.at,
                            error_type=item.reason,
                        )
                        recorded = _utc(item.at)
        # The page is persisted; only now does the cursor move (at-least-once).
        self.cursor = page.next_cursor
        refired = list(dict.fromkeys(refired_ids))
        created = [i for i in dict.fromkeys(incident_ids) if i not in refired]
        if created and self._on_incidents is not None:
            self._on_incidents(created)
        if refired and self._on_refired is not None:
            self._on_refired(refired)
        resolved = list(dict.fromkeys(resolved_ids))
        if resolved and self._on_resolved is not None:
            self._on_resolved(resolved)
        return len(page.items) + len(page.gaps)

    def run(self, stop: threading.Event, interval: float = 1.0) -> None:
        """Read until stopped; a page that cannot be persisted is logged and read again."""
        while not stop.is_set():
            try:
                busy = self.step() >= self._limit
            except Exception:
                logger.warning("alert stream page could not be applied", exc_info=True)
                busy = False
            if not busy:
                stop.wait(interval)
