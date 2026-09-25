"""The base evidence manifest: which exact persisted sources a diagnosis run may know.

Storage-independent vocabulary. A manifest entry names one authoritative row
by its persistent id; the order of entries is canonical, so two manifests with
the same membership are written in the same sequence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Canonical source types (M19-3.1 inventory), in manifest order.
SOURCE_TYPES = (
    "SNAPSHOT_CYCLE",
    "ALERT",
    "OBJECT_VERSION",
    "EVENT_VERSION",
    "LIFECYCLE",
    "CHANGE",
    "LOG",
)


@dataclass(frozen=True)
class ManifestEntry:
    """One exact persisted source: ``source_id`` is the row's persistent id."""

    source_type: str
    source_id: str

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"unknown manifest source type: {self.source_type}")
        if not self.source_id:
            raise ValueError("manifest entries need a source id")


def _natural(source_id: str) -> tuple[int, int, str]:
    return (0, int(source_id), "") if source_id.isdigit() else (1, 0, source_id)


def ordered_entries(entries: Iterable[ManifestEntry]) -> tuple[ManifestEntry, ...]:
    """Unique entries in canonical order: source type, then id (numeric ids numerically)."""
    return tuple(
        sorted(
            set(entries),
            key=lambda entry: (SOURCE_TYPES.index(entry.source_type), _natural(entry.source_id)),
        )
    )


def event_evidence_id(version_id: int) -> str:
    """RCA evidence id of one persisted Kubernetes Event version (``event_versions`` PK)."""
    return f"event:{version_id}"


__all__ = ["SOURCE_TYPES", "ManifestEntry", "event_evidence_id", "ordered_entries"]
