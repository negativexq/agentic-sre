"""M21 amendment 4: contiguous alert-channel coverage segments and their poll audit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from packages.rca.alert_coverage import (
    ALERT_COVERAGE_SOURCE,
    AlertCoverageConfig,
    AlertCoveragePoller,
    AlertmanagerError,
    alert_coverage_poller_from_environment,
)
from packages.storage.database import create_session_factory
from packages.storage.models import AlertCoveragePollRow, AlertCoverageSegmentRow, Base

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
CONFIG = AlertCoverageConfig(poll_interval=timedelta(seconds=60), max_gap_polls=2)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeReader:
    def __init__(self) -> None:
        self.failing = False

    def active_alert_count(self) -> int:
        if self.failing:
            raise AlertmanagerError("Alertmanager request failed")
        return 3


def _segments(factory: sessionmaker[Session]) -> list[tuple[Any, ...]]:
    with factory() as session:
        rows = session.scalars(
            select(AlertCoverageSegmentRow).order_by(AlertCoverageSegmentRow.segment_id)
        ).all()
        return [
            (row.status, row.started_at, row.last_success_at, row.ended_at, row.source)
            for row in rows
        ]


def _exercise(factory: sessionmaker[Session]) -> None:
    clock = Clock()
    reader = FakeReader()
    poller = AlertCoveragePoller(reader, factory, CONFIG, clock)

    def at(seconds: float) -> None:
        clock.now = T0 + timedelta(seconds=seconds)

    def minutes(value: float) -> datetime:
        return T0 + timedelta(seconds=value)

    for seconds in (0, 60, 180.001):  # 180.001: one missed slot plus jitter, still contiguous
        at(seconds)
        assert poller.poll_once().success
    at(360)  # three slots after the last success: the segment is broken
    second = poller.poll_once()
    at(420)
    reader.failing = True
    failed = poller.poll_once()
    reader.failing = False
    at(430)  # right after an explicit failure: a new segment, no tolerance
    third = poller.poll_once()

    assert _segments(factory) == [
        ("BROKEN_GAP", minutes(0), minutes(180.001), minutes(180.001), ALERT_COVERAGE_SOURCE),
        ("CLOSED_FAILURE", minutes(360), minutes(360), minutes(360), ALERT_COVERAGE_SOURCE),
        ("OPEN", minutes(430), minutes(430), None, ALERT_COVERAGE_SOURCE),
    ]
    assert not failed.success and failed.error_type == "AlertmanagerError"
    assert second.segment_id != third.segment_id
    with factory() as session:
        polls = session.scalars(select(AlertCoveragePollRow).order_by(AlertCoveragePollRow.poll_id))
        audit = [
            (row.success, row.segment_id is None, row.error_type, row.active_alerts)
            for row in polls
        ]
    assert audit == [
        (True, False, None, 3),
        (True, False, None, 3),
        (True, False, None, 3),
        (True, False, None, 3),
        (False, True, "AlertmanagerError", None),
        (True, False, None, 3),
    ]


def test_segments_follow_polls_gaps_and_failures(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    Base.metadata.create_all(engine)
    _exercise(create_session_factory(engine))


def test_a_poller_restart_after_a_long_silence_starts_a_new_segment(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    clock = Clock()
    AlertCoveragePoller(FakeReader(), factory, CONFIG, clock).poll_once()
    clock.now = T0 + timedelta(hours=1)  # the process was down; the segment is still OPEN
    AlertCoveragePoller(FakeReader(), factory, CONFIG, clock).poll_once()
    assert [row[0] for row in _segments(factory)] == ["BROKEN_GAP", "OPEN"]


def test_the_poller_is_built_only_when_alertmanager_is_configured(tmp_path: Path) -> None:
    factory: Callable[..., Any] = create_session_factory(
        create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    )
    assert alert_coverage_poller_from_environment(factory, {}) is None  # type: ignore[arg-type]
    poller = alert_coverage_poller_from_environment(
        factory,  # type: ignore[arg-type]
        {
            "SRE_ALERTMANAGER_URL": "http://alertmanager:9093",
            "SRE_ALERT_COVERAGE_POLL_SECONDS": "30",
        },
    )
    assert poller is not None
    assert poller.config.poll_interval == timedelta(seconds=30)


@pytest.mark.postgres
def test_segments_on_postgres_after_the_migration(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", postgres_url)
    command.upgrade(config, "head")
    engine = create_engine(postgres_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"alert_coverage_segments", "alert_coverage_polls"} <= tables
        _exercise(create_session_factory(engine))
        command.downgrade(config, "-1")
        assert "alert_coverage_segments" not in set(inspect(engine).get_table_names())
        command.upgrade(config, "head")
    finally:
        engine.dispose()
