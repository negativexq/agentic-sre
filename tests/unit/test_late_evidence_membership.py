"""Membership of late evidence (late-evidence-design.md §3): by the Connector's observation, not by arrival."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.storage.models import Base
from packages.storage.repositories import EventRepository, ObjectVersionRepository

T = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
WINDOW: dict[str, Any] = {"namespaces": {"shop"}, "starts_at": T - timedelta(hours=2), "ends_at": T}


def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def pod(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
        "status": {"phase": "Running"},
    }


def event(name: str, at: datetime) -> dict[str, Any]:
    return {
        "kind": "Event",
        "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
        "involvedObject": {"kind": "Pod", "name": "p", "namespace": "shop", "uid": "uid-p"},
        "reason": "Recovered",
        "lastTimestamp": at.isoformat(),
        "count": 1,
    }


def seconds(n: float) -> datetime:
    return T + timedelta(seconds=n)


def test_an_object_the_connector_saw_before_the_cutoff_is_a_member_however_late_it_arrived() -> (
    None
):
    with session() as s:
        journal = ObjectVersionRepository(s)
        journal.record(pod("late"), seconds(3), connector_observed_at=seconds(-1))  # in transit 4 s
        journal.record(pod("after"), seconds(2), connector_observed_at=seconds(1))  # seen after T
        journal.record(pod("legacy-in"), seconds(-1))  # no Connector observation: arrival rule
        journal.record(pod("legacy-out"), seconds(1))
        entries = {e.body["metadata"]["name"]: e for e in journal.history(**WINDOW)}
        assert set(entries) == {"late", "legacy-in"}
        # the engine receives the time of the observation, so its own cutoff filters agree
        assert entries["late"].observed_at == seconds(-1)
        ids = journal.history_version_ids(**WINDOW)
        assert {e.body["metadata"]["name"]: e.observed_at for e in journal.entries(ids)} == {
            "late": seconds(-1),
            "legacy-in": seconds(-1),
        }  # the replay path loads the same times


def test_an_event_the_connector_saw_before_the_cutoff_is_a_member_and_an_early_source_time_is_not_enough() -> (
    None
):
    with session() as s:
        events = EventRepository(s)
        events.record(event("closing", seconds(-2)), seconds(3), connector_observed_at=seconds(-1))
        # an old source time does not admit an Event the Connector only saw after the cutoff
        events.record(
            event("hindsight", seconds(-600)), seconds(5), connector_observed_at=seconds(4)
        )
        names = {b["metadata"]["name"] for b in events.analysis_view(**WINDOW)}
        assert names == {"closing"}
        assert {b["metadata"]["name"] for b in events.history_versions(**WINDOW)} == {"closing"}
