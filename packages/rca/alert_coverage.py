"""Alert-channel observation coverage (M21 contract §10.2, amendment 4).

The Alertmanager webhook is a notification channel: silence there is not
evidence that nothing fired, so it cannot bound what the product observed. Coverage
comes from an active, read-only poll of Alertmanager's ``GET /api/v2/alerts``:

* a successful poll is a heartbeat of the current coverage segment;
* an explicit failed poll closes the segment at its last success;
* a gap of more than ``max_gap_polls`` polling slots (rounded to the nearest slot,
  so scheduler jitter never splits a segment) breaks it;
* the next successful poll starts a new segment.

The poller never creates incidents and has no RCA authority by itself; a run
boundary later freezes the segment it was taken in.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

ALERT_COVERAGE_SOURCE = "alertmanager"
_ALERTS_PATH = "/api/v2/alerts"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class AlertmanagerError(RuntimeError):
    """A poll that did not observe the alert channel."""


class AlertmanagerProtocolError(AlertmanagerError):
    """Alertmanager answered with something that is not an alert list."""


@dataclass(frozen=True)
class AlertCoverageConfig:
    """Polling cadence and the gap, in polling slots, that breaks a segment."""

    poll_interval: timedelta = timedelta(seconds=60)
    max_gap_polls: int = 2

    def __post_init__(self) -> None:
        if self.poll_interval <= timedelta(0):
            raise ValueError("the alert coverage poll interval must be positive")
        if self.max_gap_polls < 1:
            raise ValueError("the alert coverage gap must allow at least one polling slot")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AlertCoverageConfig:
        env = os.environ if environ is None else environ
        try:
            seconds = float(env.get("SRE_ALERT_COVERAGE_POLL_SECONDS", "60"))
            max_gap_polls = int(env.get("SRE_ALERT_COVERAGE_MAX_GAP_POLLS", "2"))
        except ValueError as error:
            raise ValueError("SRE_ALERT_COVERAGE_* settings must be numeric") from error
        return cls(poll_interval=timedelta(seconds=seconds), max_gap_polls=max_gap_polls)

    def continues(self, last_success: datetime, now: datetime) -> bool:
        """Whether a success at ``now`` extends a segment last heard at ``last_success``."""
        slots = math.floor((now - last_success) / self.poll_interval + 0.5)
        return slots <= self.max_gap_polls


@dataclass(frozen=True)
class AlertmanagerConfig:
    """Where the read-only coverage client reaches Alertmanager."""

    base_url: str
    bearer_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SRE_ALERTMANAGER_URL must use http/https and include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("SRE_ALERTMANAGER_URL must not include credentials, query or fragment")
        if not 0.1 <= float(self.timeout_seconds) <= 30.0:
            raise ValueError("SRE_ALERTMANAGER_TIMEOUT_SECONDS must be between 0.1 and 30")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> AlertmanagerConfig | None:
        """``None`` when ``SRE_ALERTMANAGER_URL`` is unset: alert coverage is then unknown."""
        env = os.environ if environ is None else environ
        url = env.get("SRE_ALERTMANAGER_URL", "").strip()
        if not url:
            return None
        try:
            timeout = float(env.get("SRE_ALERTMANAGER_TIMEOUT_SECONDS", "5"))
        except ValueError as error:
            raise ValueError("SRE_ALERTMANAGER_TIMEOUT_SECONDS must be numeric") from error
        token = env.get("SRE_ALERTMANAGER_TOKEN", "").strip() or None
        return cls(url, bearer_token=token, timeout_seconds=timeout)


COVERAGE_CONTIGUOUS = "CONTIGUOUS"
COVERAGE_UNAVAILABLE = "UNAVAILABLE"


class AlertCoverageBoundaryError(ValueError):
    """A persisted alert-coverage boundary is malformed."""


@dataclass(frozen=True)
class AlertCoverageBoundary:
    """The alert-channel coverage a run froze at its boundary (M21 contract §10.2).

    ``CONTIGUOUS``: the run boundary lies in ``segment_id``, observed without a gap
    since ``observation_start`` (W). ``UNAVAILABLE``: no contiguous segment covers
    it, so W, and with it any alert-derived onset authority, is unknown.
    """

    status: str
    segment_id: int | None = None
    observation_start: datetime | None = None
    last_success: datetime | None = None

    def __post_init__(self) -> None:
        contiguous = (self.segment_id, self.observation_start, self.last_success)
        if self.status == COVERAGE_CONTIGUOUS:
            if any(item is None for item in contiguous):
                raise AlertCoverageBoundaryError("contiguous coverage needs a segment and W")
        elif self.status == COVERAGE_UNAVAILABLE:
            if any(item is not None for item in contiguous):
                raise AlertCoverageBoundaryError("unavailable coverage carries no segment")
        else:
            raise AlertCoverageBoundaryError(f"unknown alert coverage status {self.status!r}")

    @property
    def alert_observation_start(self) -> datetime | None:
        """W when the channel was contiguously observed; otherwise unknown."""
        return self.observation_start if self.status == COVERAGE_CONTIGUOUS else None

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "segment_id": self.segment_id,
            "observation_start": _iso(self.observation_start),
            "last_success": _iso(self.last_success),
        }

    @classmethod
    def from_payload(cls, value: object) -> AlertCoverageBoundary:
        """Exactly what was persisted; anything else is an error, never repaired."""
        if not isinstance(value, dict) or set(value) != {
            "status",
            "segment_id",
            "observation_start",
            "last_success",
        }:
            raise AlertCoverageBoundaryError(f"alert coverage boundary is {value!r}")
        segment = value["segment_id"]
        if segment is not None and (not isinstance(segment, int) or isinstance(segment, bool)):
            raise AlertCoverageBoundaryError(f"alert coverage segment id is {segment!r}")
        status = value["status"]
        if not isinstance(status, str):
            raise AlertCoverageBoundaryError(f"alert coverage status is {status!r}")
        return cls(
            status=status,
            segment_id=segment,
            observation_start=_parse(value["observation_start"]),
            last_success=_parse(value["last_success"]),
        )


UNAVAILABLE_COVERAGE = AlertCoverageBoundary(COVERAGE_UNAVAILABLE)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise AlertCoverageBoundaryError(f"alert coverage time is {value!r}")
    return parsed


class _Response(Protocol):
    status: int

    def read(self, limit: int) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


class _Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _Response: ...


def _default_opener(request: Request, *, timeout: float) -> _Response:
    return cast(_Response, urlopen(request, timeout=timeout))  # noqa: S310 - http(s) validated


@dataclass(frozen=True)
class AlertmanagerReader:
    """Read-only Alertmanager client: it only ever issues ``GET /api/v2/alerts``."""

    config: AlertmanagerConfig
    opener: _Opener = field(default=_default_opener, repr=False, compare=False)

    def active_alert_count(self) -> int:
        """The number of alerts Alertmanager reports now; raises when it was not observed."""
        return len(fetch_alerts(self))


def fetch_alerts(reader: AlertmanagerReader) -> list[dict[str, Any]]:
    """The alerts Alertmanager reports now (``GET /api/v2/alerts`` only); raises if unobserved."""
    config, opener = reader.config, reader.opener
    headers = {"Accept": "application/json"}
    if config.bearer_token:
        headers["Authorization"] = f"Bearer {config.bearer_token}"
    request = Request(f"{config.base_url}{_ALERTS_PATH}", headers=headers, method="GET")
    try:
        with opener(request, timeout=config.timeout_seconds) as response:
            if not 200 <= response.status < 300:
                raise AlertmanagerError(f"Alertmanager returned HTTP {response.status}")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise AlertmanagerError(f"Alertmanager returned HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise AlertmanagerError("Alertmanager request failed") from error
    if len(body) > _MAX_RESPONSE_BYTES:
        raise AlertmanagerProtocolError("Alertmanager response is too large")
    try:
        alerts = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlertmanagerProtocolError("Alertmanager returned invalid JSON") from error
    if not isinstance(alerts, list):
        raise AlertmanagerProtocolError("Alertmanager did not return an alert list")
    return [a for a in alerts if isinstance(a, dict)]


class AlertChannelReader(Protocol):
    """What the poller needs: observe the alert channel now, or raise."""

    def active_alert_count(self) -> int: ...


@dataclass(frozen=True)
class PollOutcome:
    """What one poll recorded; for logs and tests, never read by RCA."""

    success: bool
    segment_id: int | None
    error_type: str | None = None


@dataclass
class AlertCoveragePoller:
    """Polls Alertmanager and records coverage segments until stopped."""

    reader: AlertChannelReader
    session_factory: sessionmaker[Session]
    config: AlertCoverageConfig = field(default_factory=AlertCoverageConfig)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def poll_once(self) -> PollOutcome:
        from packages.storage.repositories import AlertCoverageRepository

        attempted_at = self.clock()
        try:
            active = self.reader.active_alert_count()
        except AlertmanagerError as error:
            completed_at = self.clock()
            with self.session_factory() as session:
                AlertCoverageRepository(session).record_failure(
                    source=ALERT_COVERAGE_SOURCE,
                    attempted_at=attempted_at,
                    completed_at=completed_at,
                    error_type=type(error).__name__,
                )
            return PollOutcome(False, None, type(error).__name__)
        completed_at = self.clock()
        with self.session_factory() as session:
            segment_id = AlertCoverageRepository(session).record_success(
                source=ALERT_COVERAGE_SOURCE,
                attempted_at=attempted_at,
                completed_at=completed_at,
                active_alerts=active,
                config=self.config,
            )
        return PollOutcome(True, segment_id)

    def run(self, stop: threading.Event) -> None:
        """Poll every interval until ``stop`` is set; a storage error is logged and retried."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.warning("alert coverage poll could not be recorded", exc_info=True)
            stop.wait(self.config.poll_interval.total_seconds())


def alert_coverage_poller_from_environment(
    session_factory: sessionmaker[Session], environ: Mapping[str, str] | None = None
) -> AlertCoveragePoller | None:
    """A poller when ``SRE_ALERTMANAGER_URL`` is set; otherwise alert coverage stays unknown."""
    alertmanager = AlertmanagerConfig.from_environment(environ)
    if alertmanager is None:
        return None
    return AlertCoveragePoller(
        reader=AlertmanagerReader(alertmanager),
        session_factory=session_factory,
        config=AlertCoverageConfig.from_environment(environ),
    )


__all__ = [
    "ALERT_COVERAGE_SOURCE",
    "COVERAGE_CONTIGUOUS",
    "COVERAGE_UNAVAILABLE",
    "UNAVAILABLE_COVERAGE",
    "AlertChannelReader",
    "AlertCoverageBoundary",
    "AlertCoverageBoundaryError",
    "AlertCoverageConfig",
    "AlertCoveragePoller",
    "AlertmanagerConfig",
    "AlertmanagerError",
    "AlertmanagerProtocolError",
    "AlertmanagerReader",
    "PollOutcome",
    "alert_coverage_poller_from_environment",
]
