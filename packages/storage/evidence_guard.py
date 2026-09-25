"""Runtime guard: authoritative evidence rows are never updated or deleted by a Session.

Evidence tables are append-only (M19 §1.5, I1). Static tests catch raw SQL and
SQLAlchemy ``update()``/``delete()`` statements; this guard catches what they
cannot see, an ORM attribute assignment or ``session.delete()`` on a loaded
evidence row, at flush time. It hooks ORM Sessions only, so migrations and the
retention module's explicit Core deletes are unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    EventVersionRow,
    LifecycleObservationRow,
    LogObservationRow,
    ObjectVersionRow,
)

AUTHORITATIVE_ROWS: tuple[type[Any], ...] = (
    EventVersionRow,
    ObjectVersionRow,
    LifecycleObservationRow,
    ChangeRecordRow,
    LogObservationRow,
)
# Still updated in place by alert ingestion until M19-3.6a freezes alert
# content in the evidence manifest; listed so the exception stays visible.
TRANSITIONAL_MUTABLE_ROWS: dict[type[Any], str] = {
    AlertRow: "M19-3.6a freezes alert content in the evidence manifest",
}


class AuthoritativeEvidenceMutation(RuntimeError):
    """An ORM flush tried to change or remove an append-only evidence row."""


def _guard(session: Session, _context: Any, _instances: Any) -> None:
    for item in session.deleted:
        if isinstance(item, AUTHORITATIVE_ROWS):
            raise AuthoritativeEvidenceMutation(
                f"{type(item).__name__} is append-only evidence and cannot be deleted"
            )
    for item in session.dirty:
        if isinstance(item, AUTHORITATIVE_ROWS) and session.is_modified(item):
            raise AuthoritativeEvidenceMutation(
                f"{type(item).__name__} is append-only evidence and cannot be updated"
            )


if not event.contains(Session, "before_flush", _guard):
    event.listen(Session, "before_flush", _guard)


__all__ = [
    "AUTHORITATIVE_ROWS",
    "TRANSITIONAL_MUTABLE_ROWS",
    "AuthoritativeEvidenceMutation",
]
