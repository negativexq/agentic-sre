"""Server-sent event streams for the console.

These stream only transitions in *persisted* state — a new incident, a newly
appended timeline event, a stored diagnosis. The server polls its own database
on a short interval and emits an event when a cheap fingerprint of that state
changes; nothing here invents progress the engine did not record. Clients use
the event as a signal to refetch the authoritative DTO, so the persisted store
stays the single source of truth (see docs/ui/product-contract.md, G5).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from packages.storage.models import DiagnosisRow, IncidentEventRow, IncidentRow

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0


def _global_fingerprint(session: Session) -> tuple[int, int, int]:
    incidents = session.scalar(select(func.count()).select_from(IncidentRow)) or 0
    events = session.scalar(select(func.count()).select_from(IncidentEventRow)) or 0
    diagnoses = session.scalar(select(func.count()).select_from(DiagnosisRow)) or 0
    return (incidents, events, diagnoses)


def _incident_fingerprint(session: Session, incident_id: object) -> tuple[int, int]:
    events = (
        session.scalar(
            select(func.count())
            .select_from(IncidentEventRow)
            .where(IncidentEventRow.incident_id == incident_id)
        )
        or 0
    )
    diagnoses = (
        session.scalar(
            select(func.count())
            .select_from(DiagnosisRow)
            .where(DiagnosisRow.incident_id == incident_id)
        )
        or 0
    )
    return (events, diagnoses)


async def _pump(
    session_factory: sessionmaker[Session],
    fingerprint: Callable[[Session], tuple[int, ...]],
    event_name: str,
) -> AsyncIterator[bytes]:
    """Emit an SSE ``event_name`` whenever the fingerprint changes.

    The first tick always emits so a fresh subscriber reconciles immediately;
    an idle stream sends a heartbeat comment so proxies keep it open and the
    client's connection state stays honest.
    """
    last: tuple[int, ...] | None = None
    idle = 0.0
    # Emit an initial retry hint so EventSource reconnect backoff is bounded.
    yield b"retry: 3000\n\n"
    while True:
        try:
            with session_factory() as session:
                current = await asyncio.to_thread(fingerprint, session)
        except Exception:  # noqa: BLE001 - a transient DB error must not kill the stream
            current = last if last is not None else ()
        if current != last:
            last = current
            payload = json.dumps({"fingerprint": list(current)})
            yield f"event: {event_name}\ndata: {payload}\n\n".encode()
            idle = 0.0
        else:
            idle += POLL_SECONDS
            if idle >= HEARTBEAT_SECONDS:
                idle = 0.0
                yield b": ping\n\n"
        await asyncio.sleep(POLL_SECONDS)


def global_stream(session_factory: sessionmaker[Session]) -> AsyncIterator[bytes]:
    """Signal any change across incidents, events or diagnoses."""
    return _pump(session_factory, _global_fingerprint, "state")


def incident_stream(
    session_factory: sessionmaker[Session], incident_id: object
) -> AsyncIterator[bytes]:
    """Signal a new event or stored diagnosis for one incident."""
    return _pump(
        session_factory, lambda session: _incident_fingerprint(session, incident_id), "incident"
    )
