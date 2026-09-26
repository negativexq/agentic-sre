"""Read-only reload helpers for persisted provider tape data."""

from __future__ import annotations

from sqlalchemy.orm import Session

from packages.rca.tape import TapeDigestEntry, compute_tape_digest
from packages.storage.repositories import InvestigationReadRepository


def load_tape_digest(session: Session, run_id: str) -> str:
    """Reload a run's persisted tape and compute its canonical digest."""
    rows = InvestigationReadRepository(session).list_for_run(run_id)
    return compute_tape_digest(
        TapeDigestEntry(
            sequence=row.sequence,
            caller_class=row.caller_class,
            query_key=row.query_key,
            status=row.status,
            evidence_ids=tuple(row.evidence_ids),
        )
        for row in rows
    )


__all__ = ["load_tape_digest"]
