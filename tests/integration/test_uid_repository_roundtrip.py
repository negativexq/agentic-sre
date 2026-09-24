"""Repository round trips for persisted Kubernetes instance UIDs."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.rca.live import LiveSource
from packages.rca.model import EntityRef
from packages.storage.models import Base, EventVersionRow, ObjectVersionRow
from packages.storage.repositories import EventRepository, ObjectVersionRepository

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _object_body(*, uid: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "payment-abc", "namespace": "sre-demo"}
    if uid is not None:
        metadata["uid"] = uid
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata}


def _event_body(*, involved_uid: str | None) -> dict[str, Any]:
    involved: dict[str, Any] = {
        "kind": "Pod",
        "namespace": "sre-demo",
        "name": "payment-abc",
    }
    if involved_uid is not None:
        involved["uid"] = involved_uid
    return {
        "kind": "Event",
        "metadata": {"name": "event-1", "namespace": "sre-demo", "uid": "uid-event-X"},
        "involvedObject": involved,
        "reason": "BackOff",
        "type": "Warning",
    }


def _engine(tmp_path: Path, name: str) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine


def test_object_uid_write_and_column_authoritative_read_round_trip(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "object-uid.db")
    entity = EntityRef(kind="Pod", name="payment-abc", namespace="sre-demo")

    with Session(engine) as session:
        repository = ObjectVersionRepository(session)
        assert repository.record(_object_body(uid="uid-object-A"), T0)
        row = session.scalar(select(ObjectVersionRow))
        assert row is not None
        assert row.uid == "uid-object-A"

        # Deliberately diverge the JSON copy to prove reads use the UID column.
        row.body = _object_body(uid="uid-body-conflict")
        session.commit()
        journal = repository.history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=1)
        )
        source = LiveSource(
            incident="object-uid-round-trip",
            alert_items=[],
            journal=journal,
            current_objects=[],
            event_bodies=[],
            current_is_live=False,
            observed_at=T0 + timedelta(minutes=1),
        )
        version = source.object_history()[entity][0]
        assert version.uid == "uid-object-A"

    engine.dispose()


def test_missing_object_uid_remains_null_and_maps_to_none(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "object-uid-missing.db")
    entity = EntityRef(kind="Pod", name="payment-abc", namespace="sre-demo")

    with Session(engine) as session:
        repository = ObjectVersionRepository(session)
        assert repository.record(_object_body(uid=None), T0)
        row = session.scalar(select(ObjectVersionRow))
        assert row is not None
        assert row.uid is None
        journal = repository.history(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=1)
        )
        source = LiveSource(
            incident="object-uid-missing",
            alert_items=[],
            journal=journal,
            current_objects=[],
            event_bodies=[],
            current_is_live=False,
            observed_at=T0 + timedelta(minutes=1),
        )
        assert source.object_history()[entity][0].uid is None
    engine.dispose()


def test_event_involved_uid_write_and_column_authoritative_read_round_trip(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "event-uid.db")

    with Session(engine) as session:
        repository = EventRepository(session)
        body = _event_body(involved_uid="uid-pod-A")
        assert repository.record(body, T0)
        row = session.scalar(select(EventVersionRow))
        assert row is not None
        assert row.involved_uid == "uid-pod-A"
        assert row.body["metadata"]["uid"] == "uid-event-X"

        # The event object's UID is distinct from its involved object's UID.
        row.body = _event_body(involved_uid="uid-body-conflict")
        session.commit()
        bodies = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=1)
        )
        source = LiveSource(
            incident="event-uid-round-trip",
            alert_items=[],
            journal=[],
            current_objects=[],
            event_bodies=bodies,
            current_is_live=False,
            observed_at=T0 + timedelta(minutes=1),
        )
        event = source.events()[0]
        assert event.involved_uid == "uid-pod-A"
        assert event.involved_uid != "uid-event-X"
    engine.dispose()


def test_missing_event_involved_uid_does_not_fall_back_to_event_uid(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "event-uid-missing.db")

    with Session(engine) as session:
        repository = EventRepository(session)
        assert repository.record(_event_body(involved_uid=None), T0)
        row = session.scalar(select(EventVersionRow))
        assert row is not None
        assert row.involved_uid is None
        assert row.body["metadata"]["uid"] == "uid-event-X"
        bodies = repository.analysis_view(
            namespaces={"sre-demo"}, starts_at=T0, ends_at=T0 + timedelta(minutes=1)
        )
        source = LiveSource(
            incident="event-uid-missing",
            alert_items=[],
            journal=[],
            current_objects=[],
            event_bodies=bodies,
            current_is_live=False,
            observed_at=T0 + timedelta(minutes=1),
        )
        assert source.events()[0].involved_uid is None
    engine.dispose()
