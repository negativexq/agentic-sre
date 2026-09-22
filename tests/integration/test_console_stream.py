"""Tests for the console SSE streams (M5)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.control_plane.console import stream as stream_mod
from apps.control_plane.console.stream import (
    _global_fingerprint,
    _incident_fingerprint,
    _pump,
)
from packages.contracts import (
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.storage.models import Base
from packages.storage.repositories import IncidentEventRepository, IncidentRepository

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _make_incident() -> Incident:
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="stream test",
        created_at=T0,
        updated_at=T0,
    )


def test_fingerprints_track_persisted_state() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert _global_fingerprint(session) == (0, 0, 0)
        incident = _make_incident()
        IncidentRepository(session).create(incident)
        # One incident and its INCIDENT_CREATED event.
        assert _global_fingerprint(session) == (1, 1, 0)
        assert _incident_fingerprint(session, incident.incident_id) == (1, 0)

        IncidentEventRepository(session).append(
            IncidentEvent(
                incident_id=incident.incident_id,
                event_type=IncidentEventType.DIAGNOSIS_STARTED,
                timestamp=T0 + timedelta(seconds=1),
                correlation_id=incident.correlation_id,
                payload={"run_id": "r1"},
            )
        )
        assert _incident_fingerprint(session, incident.incident_id) == (2, 0)
    engine.dispose()


def _collect(gen: object, count: int) -> list[bytes]:
    async def run() -> list[bytes]:
        out: list[bytes] = []
        async for chunk in gen:  # type: ignore[attr-defined]
            out.append(chunk)
            if len(out) >= count:
                break
        return out

    return asyncio.run(run())


def test_pump_emits_retry_then_initial_then_on_change(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(stream_mod, "POLL_SECONDS", 0.0)

    values = iter([(1,), (1,), (2,)])
    last = [(1,)]

    def fingerprint(_: Session) -> tuple[int, ...]:
        try:
            last[0] = next(values)
        except StopIteration:
            pass
        return last[0]

    @contextmanager
    def fake_factory() -> Iterator[Session]:
        yield None  # type: ignore[misc]

    gen = _pump(fake_factory, fingerprint, "state")  # type: ignore[arg-type]
    chunks = _collect(gen, 3)

    assert chunks[0] == b"retry: 3000\n\n"
    # First real read emits because last starts as None inside the pump.
    assert chunks[1].startswith(b"event: state\n")
    assert b'"fingerprint": [1]' in chunks[1]
    # The (1,) -> (1,) step is unchanged and yields nothing; the next change to
    # (2,) emits the third chunk.
    assert chunks[2].startswith(b"event: state\n")
    assert b'"fingerprint": [2]' in chunks[2]
