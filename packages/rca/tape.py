"""Canonical, storage-independent provider tape digest helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class TapeDigestEntry:
    """The exact persisted fields that participate in a tape digest."""

    sequence: int
    caller_class: str
    query_key: str
    status: str
    evidence_ids: tuple[str, ...]


def compute_tape_digest(entries: Iterable[TapeDigestEntry]) -> str:
    """Hash canonical JSON of tape entries in authoritative sequence order."""
    canonical_rows = [
        [
            entry.sequence,
            entry.caller_class,
            entry.query_key,
            entry.status,
            sorted(entry.evidence_ids),
        ]
        for entry in sorted(entries, key=lambda item: item.sequence)
    ]
    canonical = json.dumps(
        canonical_rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["TapeDigestEntry", "compute_tape_digest"]
