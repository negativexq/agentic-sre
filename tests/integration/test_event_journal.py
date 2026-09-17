"""Replay semantics for the append-only Kubernetes Event journal."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.rca.live import LiveSource
from packages.rca.signals import failure_findings
from packages.storage.models import Base
from packages.storage.repositories import EventRepository

T0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def _event(uid: str, count: int, last_minutes: int, *, first_minutes: int = 2) -> dict[str, Any]:
    return {
        "kind": "Event",
        "metadata": {
            "name": f"{uid}.sre-demo",
            "namespace": "sre-demo",
            "uid": uid,
        },
        "involvedObject": {
            "kind": "Pod",
            "name": "checkout-abc",
            "namespace": "sre-demo",
            "uid": "pod-uid",
        },
        "reason": "BackOff",
        "type": "Warning",
        "message": "back-off restarting failed container",
        "firstTimestamp": (T0 + timedelta(minutes=first_minutes)).isoformat(),
        "lastTimestamp": (T0 + timedelta(minutes=last_minutes)).isoformat(),
        "count": count,
    }


def test_event_versions_survive_gc_but_resolved_replay_observes_only_known_state(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = EventRepository(session)
        assert repository.record(_event("event-1", 1, 2), T0 + timedelta(minutes=3))
        assert repository.record(_event("event-1", 20, 30), T0 + timedelta(minutes=30))

        raw = repository.history_versions(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=40)
        )
        visible_at_resolution = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=15)
        )
    engine.dispose()

    assert len(raw) == 2
    assert [body["count"] for body in visible_at_resolution] == [1]


def test_event_observed_during_lookback_is_kept_when_it_started_earlier(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = EventRepository(session)
        assert repository.record(
            _event("event-before-lookback", 1, -59, first_minutes=-60),
            T0 + timedelta(minutes=1),
        )
        visible = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=5)
        )
    engine.dispose()

    assert [body["count"] for body in visible] == [1]


def test_coalesced_versions_are_one_analysis_event_and_not_double_counted(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = EventRepository(session)
        for count in (1, 2, 3):
            assert repository.record(
                _event("event-1", count, count + 1), T0 + timedelta(minutes=count + 1)
            )
        bodies = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=10)
        )
    engine.dispose()

    source = LiveSource(
        incident="coalesced",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=bodies,
    )
    findings = failure_findings(source.events())
    assert len(bodies) == 1
    assert findings[0].details["count"] == 3


def test_distinct_event_uids_remain_distinct_logical_events(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = EventRepository(session)
        assert repository.record(_event("event-a", 3, 3), T0 + timedelta(minutes=4))
        assert repository.record(_event("event-b", 2, 4), T0 + timedelta(minutes=5))
        bodies = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=10)
        )
    engine.dispose()

    source = LiveSource(
        incident="distinct",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=bodies,
    )
    findings = failure_findings(source.events())
    assert len(bodies) == 2
    assert findings[0].details["count"] == 5
