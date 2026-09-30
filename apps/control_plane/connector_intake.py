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
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from packages.connector import wire
from packages.connector.client import ConnectorClient, ConnectorError
from packages.contracts import AlertmanagerAlertPayload
from packages.incident import IncidentManager, normalize_alert
from packages.rca.alert_coverage import ALERT_COVERAGE_SOURCE, AlertCoverageConfig
from packages.storage.models import AlertCoveragePollRow
from packages.storage.repositories import AlertCoverageRepository

logger = logging.getLogger(__name__)


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_limit: int = wire.MAX_BATCH,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._config = config or AlertCoverageConfig()
        self._on_incidents = on_incidents
        self._clock = clock
        self._limit = page_limit
        self.cursor: str | None = None
        self._available: bool | None = None  # log a change of state once, not every second

    def _latest_poll(self, session: Session) -> datetime | None:
        latest = session.scalar(
            select(func.max(AlertCoveragePollRow.completed_at)).where(
                AlertCoveragePollRow.source == ALERT_COVERAGE_SOURCE
            )
        )
        return _utc(latest) if latest is not None else None

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
        with self._session_factory() as session:
            repository = AlertCoverageRepository(session)
            recorded = self._latest_poll(session)
            for item in page.ordered():
                if isinstance(item, wire.AlertItem):
                    try:
                        payload = AlertmanagerAlertPayload.model_validate(item.alert)
                        incident = IncidentManager(session).ingest(
                            normalize_alert(payload), now=self._clock()
                        )
                    except ValueError:
                        logger.warning(
                            "skipping an alert the intake cannot normalize", exc_info=True
                        )
                        continue
                    incident_ids.append(incident.incident_id)
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
        if incident_ids and self._on_incidents is not None:
            self._on_incidents(list(dict.fromkeys(incident_ids)))
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
