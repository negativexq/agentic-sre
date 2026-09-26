"""The base evidence manifest: which exact persisted sources a diagnosis run may know.

Storage-independent vocabulary. A manifest entry names one authoritative row
by its persistent id; the order of entries is canonical, so two manifests with
the same membership are written in the same sequence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any

from packages.rca.model import Alert

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
    """One exact persisted source: ``source_id`` is the row's persistent id.

    ``payload`` freezes a source's content at manifest time when its row can
    still change (alerts). It is not part of the entry's identity.
    """

    source_type: str
    source_id: str
    payload: Mapping[str, Any] | None = field(default=None, compare=False, hash=False)

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


def manifest_membership_digest(membership: Iterable[tuple[str, str]]) -> str:
    """Hash only the canonical ``source_type:source_id`` membership pairs."""
    canonical = "\n".join(
        sorted(f"{source_type}:{source_id}" for source_type, source_id in membership)
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def event_evidence_id(version_id: int) -> str:
    """RCA evidence id of one persisted Kubernetes Event version (``event_versions`` PK)."""
    return f"event:{version_id}"


def alert_from_payload(payload: Mapping[str, Any]) -> Alert:
    """An RCA alert built from the content a run's manifest froze."""
    return Alert(
        name=payload["alert_name"],
        service=payload["service"],
        namespace=payload["namespace"],
        starts_at=datetime.fromisoformat(payload["starts_at"]),
        labels={**payload["labels"], "service_name": payload["service"]},
    )


__all__ = [
    "SOURCE_TYPES",
    "ManifestEntry",
    "alert_from_payload",
    "event_evidence_id",
    "manifest_membership_digest",
    "ordered_entries",
]
